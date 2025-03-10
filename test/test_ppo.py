import unittest

import gymnasium as gym
from stable_baselines3.common.buffers import RolloutBuffer
from stable_baselines3.common.env_util import make_vec_env
from rlopt.agent.ppo import PPO
from rlopt.envs.gymlike import make_mujoco_env, make_gym_env

import hydra
from omegaconf import DictConfig
from torchrl.envs import GymEnv
from torchrl.record.loggers import WandbLogger


class TestCustomPPO(unittest.TestCase):
    def test_direct_training(self):

        @hydra.main(config_path=".", config_name="test_config", version_base=None)
        def train(cfg: DictConfig) -> None:
            env = make_gym_env(
                "HalfCheetah-v4",
                parallel=True,
                num_workers=cfg.env.num_envs,
                device="cpu",
                from_pixels=False,
            )

            agent = PPO(
                env=env,
                config=cfg,
                logger=WandbLogger,
            )

            agent.train()

        train()


if __name__ == "__main__":
    unittest.main()
