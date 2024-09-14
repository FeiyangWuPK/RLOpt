from setuptools import setup, find_packages

setup(
    name="RLOpt",
    version="0.1.0",
    description="A reinforcement learning optimization project",
    author="Feiyang Wu",
    author_email="feiyangwu@gatech.edu",
    url="https://github.com/FeiyangWuPK/RLOpt",
    packages=find_packages(),
    install_requires=[
        "numpy",
        "gymnasium",
        "torch>=2.2",
        "tensorboard",
        "tensordict",
        "tqdm",
        "rich",
        "pyyaml",
        "stable-baselines3",
        "wandb",
        "sb3-contrib",
        "transforms3d",
        "rsl_rl",
        "empy==3.3.4",
        "lark",
    ],
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
)
