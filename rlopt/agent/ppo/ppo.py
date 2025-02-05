"""
Only supports MuJoCo environments for now
"""

from typing import List, Tuple, Dict, Any, Optional, Union

import tqdm
import numpy as np
import torch
from torch import nn
from tensordict import TensorDict
from torch.optim.optimizer import Optimizer as Optimizer

from tensordict.nn import (
    AddStateIndependentNormalScale,
    TensorDictModule,
    set_composite_lp_aggregate,
)

from torchrl.modules import MLP, ProbabilisticActor, TanhNormal, ValueOperator
from torchrl.data import ReplayBuffer
from torchrl.data import LazyTensorStorage, TensorDictReplayBuffer
from torchrl.data.replay_buffers.samplers import SamplerWithoutReplacement
from torchrl.envs import EnvBase, TransformedEnv, ExplorationType
from torchrl.record import CSVLogger, TensorboardLogger, WandbLogger
from torchrl.record.loggers.common import Logger
from torchrl.collectors import SyncDataCollector
from torchrl.data.replay_buffers import LazyMemmapStorage, ReplayBuffer
from torchrl.objectives import ClipPPOLoss, group_optimizers
from torchrl.objectives.value.advantages import GAE
from torchrl._utils import timeit, compile_with_warmup

from omegaconf import DictConfig
from rlopt.common.base_class import BaseAlgorithm

set_composite_lp_aggregate(True).set()


class PPO(BaseAlgorithm):

    def __init__(
        self,
        env: EnvBase,
        config: DictConfig,
        policy: Optional[nn.Module] = None,
        value_net: Optional[nn.Module] = None,
        q_net: Optional[nn.Module] = None,
        reward_estimator: Optional[nn.Module] = None,
        replay_buffer: type[ReplayBuffer] = ReplayBuffer,
        logger: type[Logger] = TensorboardLogger,
        **kwargs,
    ):
        super().__init__(
            env,
            config,
            policy,
            value_net,
            q_net,
            reward_estimator,
            replay_buffer,
            logger,
            **kwargs,
        )

        # construct the advantage module
        self.adv_module = self._construct_adv_module()

        # Compile if requested
        self._compile_components()

    def _construct_policy(self) -> nn.Module:
        policy_config = self.config.policy
        # for PPO, we use a probabilistic actor
        self.env: TransformedEnv
        # Define input shape
        input_shape = self.env.observation_spec["observation"].shape

        # Define policy output distribution class
        num_outputs = self.env.action_spec_unbatched.shape[-1]  # type: ignore
        distribution_class = TanhNormal
        distribution_kwargs = {
            "low": self.env.action_spec_unbatched.space.low.to(self.device),  # type: ignore
            "high": self.env.action_spec_unbatched.space.high.to(self.device),  # type: ignore
            "tanh_loc": False,
        }

        # Define policy architecture
        policy_mlp = MLP(
            in_features=input_shape[-1],
            activation_class=torch.nn.Tanh,
            out_features=num_outputs,  # predict only loc
            num_cells=policy_config.num_cells,
            device=self.device,
        )

        # Initialize policy weights
        for layer in policy_mlp.modules():
            if isinstance(layer, torch.nn.Linear):
                torch.nn.init.orthogonal_(layer.weight, 1.0)  # type: ignore
                layer.bias.data.zero_()

        # Add state-independent normal scale
        policy_mlp = torch.nn.Sequential(
            policy_mlp,
            AddStateIndependentNormalScale(
                self.env.action_spec_unbatched.shape[-1], scale_lb=1e-8  # type:ignore
            ).to(self.device),
        )

        # Add probabilistic sampling of the actions
        policy_module = ProbabilisticActor(
            TensorDictModule(
                module=policy_mlp,
                in_keys=["observation"],
                out_keys=["loc", "scale"],
            ),
            in_keys=["loc", "scale"],
            spec=self.env.full_action_spec_unbatched.to(self.device),
            distribution_class=distribution_class,
            distribution_kwargs=distribution_kwargs,
            return_log_prob=True,
            default_interaction_type=ExplorationType.RANDOM,
        )

        return policy_module

    def _construct_value_function(self) -> nn.Module:
        self.env: TransformedEnv
        value_net_config = self.config.value_net
        # Define input shape
        input_shape = self.env.observation_spec["observation"].shape
        # Define value architecture
        value_mlp = MLP(
            in_features=input_shape[-1],
            activation_class=torch.nn.Tanh,
            out_features=1,
            num_cells=value_net_config.num_cells,
            device=self.device,
        )

        # Initialize value weights
        for layer in value_mlp.modules():
            if isinstance(layer, torch.nn.Linear):
                torch.nn.init.orthogonal_(layer.weight, 0.01)  # type: ignore
                layer.bias.data.zero_()

        # Define value module
        value_module = ValueOperator(
            value_mlp,
            in_keys=["observation"],
        )

        return value_module

    def _construct_loss_module(self) -> nn.Module:
        loss_config = self.config.loss
        self.policy: ProbabilisticActor
        self.value_net: ValueOperator
        loss_module = ClipPPOLoss(
            actor_network=self.policy,
            critic_network=self.value_net,
            clip_epsilon=loss_config.clip_epsilon,
            loss_critic_type=loss_config.loss_critic_type,
            entropy_coef=loss_config.entropy_coef,
            critic_coef=loss_config.critic_coef,
            normalize_advantage=True,
        )
        return loss_module

    def _configure_optimizers(self) -> torch.optim.Optimizer:
        # Create optimizers
        actor_optim = torch.optim.Adam(
            self.policy.parameters(),
            lr=torch.tensor(self.config.optim.lr, device=self.device),
            eps=1e-5,
        )
        critic_optim = torch.optim.Adam(
            self.value_net.parameters(),
            lr=torch.tensor(self.config.optim.lr, device=self.device),
            eps=1e-5,
        )
        optim = group_optimizers(actor_optim, critic_optim)
        del actor_optim, critic_optim
        return optim

    def _construct_adv_module(self) -> nn.Module:
        self.value_net: ValueOperator
        # Create loss and adv modules
        adv_module = GAE(
            gamma=self.config.loss.gamma,
            lmbda=self.config.loss.gae_lambda,
            value_network=self.value_net,
            average_gae=False,
            device=self.device,
            vectorized=not self.config.compile.compile,
        )
        return adv_module

    def _construct_data_buffer(self) -> ReplayBuffer:
        # Create data buffer
        cfg = self.config
        sampler = SamplerWithoutReplacement()
        data_buffer = TensorDictReplayBuffer(
            storage=LazyTensorStorage(
                cfg.collector.frames_per_batch,
                compilable=cfg.compile.compile,  # type: ignore
                device=self.device,
            ),
            sampler=sampler,
            batch_size=cfg.loss.mini_batch_size,
            compilable=cfg.compile.compile,
        )
        return data_buffer

    def _compile_components(self):
        compile_mode = None
        cfg = self.config
        if cfg.compile.compile:
            compile_mode = cfg.compile.compile_mode
            if compile_mode in ("", None):
                if cfg.compile.cudagraphs:
                    compile_mode = "default"
                else:
                    compile_mode = "reduce-overhead"

            self.update = torch.compile(self.update, mode=compile_mode)
            self.adv_module = torch.compile(self.adv_module, mode=compile_mode)

    def update(self, batch, num_network_updates) -> Tuple[TensorDict, int]:
        self.optim: Optimizer
        self.optim.zero_grad(set_to_none=True)

        # Linearly decrease the learning rate and clip epsilon
        alpha = torch.ones((), device=self.device) * self.config.optim.lr
        if self.config.optim.anneal_lr:
            alpha = 1 - (num_network_updates / self.total_network_updates)
            for group in self.optim.param_groups:
                group["lr"] = self.config.optim.lr * alpha

        if self.config.loss.clip_epsilon:
            self.loss_module.clip_epsilon.copy_(self.config.loss.clip_epsilon * alpha)  # type: ignore

        num_network_updates = num_network_updates + 1

        # Forward pass PPO loss
        loss = self.loss_module(batch)
        critic_loss = loss["loss_critic"]
        actor_loss = loss["loss_objective"] + loss["loss_entropy"]
        total_loss = critic_loss + actor_loss

        # Backward pass
        total_loss.backward()

        # Update the networks
        self.optim.step()
        return loss.detach().set("alpha", alpha), num_network_updates

    def predict(self, obs: Union[torch.Tensor, np.ndarray]) -> torch.Tensor:
        """Predict action and value given observation"""
        self.policy: ProbabilisticActor
        self.value_net: ValueOperator
        obs = torch.as_tensor([obs], device=self.device)
        self.policy.eval()
        with torch.inference_mode():
            td = TensorDict(
                {"observation": obs},
                batch_size=[1],
                device=self.policy.device,
            )
            output = self.policy(td).get("action")

        return output
