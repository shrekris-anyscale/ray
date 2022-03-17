import argparse

import ray
from ray import tune
from ray.ml.train.integrations.torch import TorchTrainer

from torch_linear_dataset_example import train_func, get_datasets


def tune_linear(num_workers, num_samples, use_gpu):
    train_dataset, val_dataset = get_datasets()

    config = {"lr": 1e-2, "hidden_size": 1, "batch_size": 4, "epochs": 3}

    scaling_config = {"num_workers": num_workers, "use_gpu": use_gpu}

    trainer = TorchTrainer(
        train_loop_per_worker=train_func,
        train_loop_config=config,
        scaling_config=scaling_config,
        datasets={"train": train_dataset, "validation": val_dataset},
    )

    # TODO(amog/xwjiang): Replace with Tuner.fit.
    analysis = tune.run(
        trainer.as_trainable(),
        num_samples=num_samples,
        config={
            "train_loop_config": {
                "lr": tune.loguniform(1e-4, 1e-1),
                "batch_size": tune.choice([4, 16, 32]),
                "epochs": 3,
            }
        },
    )
    results = analysis.get_best_config(metric="loss", mode="min")
    print(results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        default=False,
        help="Finish quickly for testing.",
    )
    parser.add_argument(
        "--address", required=False, type=str, help="the address to use for Ray"
    )
    parser.add_argument(
        "--num-workers",
        "-n",
        type=int,
        default=2,
        help="Sets number of workers for training.",
    )
    parser.add_argument(
        "--num-samples",
        type=int,
        default=2,
        help="Sets number of samples for training.",
    )
    parser.add_argument(
        "--use-gpu", action="store_true", default=False, help="Use GPU for training."
    )

    args = parser.parse_args()

    if args.smoke_test:
        # 2 workers, 1 for trainer, 1 for datasets
        ray.init(num_cpus=4)
        tune_linear(num_workers=2, num_samples=1, use_gpu=False)
    else:
        ray.init(address=args.address)
        tune_linear(
            num_workers=args.num_workers,
            use_gpu=args.use_gpu,
            num_samples=args.num_samples,
        )
