import os
import collections
from pathlib import Path

import numpy as np
import tensorflow as tf
import tensorflow_datasets as tfds
import tensorflow_federated as tff

from .helpers import create_budgets, get_sampling_rates_per_client
from idputils import get_weights
from train import train, save_train_results, sample_clients

SVHN_DIR = os.path.join(os.path.expanduser("~"), ".tff/svhn")
SVHN_CLIENTS_PER_ROUND = 100
SVHN_ROUNDS = 100
SVHN_DELTA = 1e-4  # I created the dataset with 725 clients

def _load_svhn():
    svhn_spec = {
        'image': tf.TensorSpec((32, 32, 3), dtype=tf.int64),
        'label': tf.TensorSpec((), dtype=tf.int64),
    }
    train_client_data =  tff.simulation.datasets.load_and_parse_sql_client_data(str(Path(SVHN_DIR) / 'train.sqlite'), element_spec=svhn_spec, split_name=None)
    test_client_data = tff.simulation.datasets.load_and_parse_sql_client_data(str(Path(SVHN_DIR) / 'test.sqlite'), element_spec=svhn_spec, split_name=None)
    return train_client_data, test_client_data


def _get_dataset():
    train_ds, test_ds = _load_svhn()
    def element_fn(element):
        return collections.OrderedDict(
            x=element['image'], y=element['label']
        )

    def preprocess_train_dataset(dataset):
        # Use buffer_size same as the maximum client dataset size,
        # currently 138 for my SVHN dataset
        return (
            dataset
            .map(element_fn)
            .shuffle(buffer_size=138)
            .repeat(1)
            .batch(32, drop_remainder=False)
        )

    def preprocess_test_dataset(dataset):
        return dataset.map(element_fn).batch(128, drop_remainder=False)

    svhn_train = train_ds.preprocess(preprocess_train_dataset)
    svhn_test = preprocess_test_dataset(
        test_ds.create_tf_dataset_from_all_clients()
    )
    
    return svhn_train, svhn_test


def _get_model(input_spec):
    model = tf.keras.models.Sequential([
        tf.keras.layers.Reshape(input_shape=(32, 32, 3), target_shape=(32 * 32 * 3,)),
        tf.keras.layers.Dense(200, activation=tf.nn.relu),
        tf.keras.layers.Dense(200, activation=tf.nn.relu),
        tf.keras.layers.Dense(10)
    ])
    return tff.learning.models.from_keras_model(
        keras_model=model,
        loss=tf.keras.losses.SparseCategoricalCrossentropy(from_logits=True),
        input_spec=input_spec,
        metrics=[tf.keras.metrics.SparseCategoricalAccuracy()]
    )


def run_svhn(save_dir, budgets, budget_ratios):
    train_ds, test_ds = _get_dataset()
    budgets_per_client = create_budgets(
        num_clients=len(train_ds.client_ids),
        possible_budgets=budgets,
        budget_ratios=budget_ratios,
    )
    noise_multiplier, qs_per_budget = get_weights(
        pp_budgets=budgets_per_client,
        target_delta=SVHN_DELTA,
        default_sample_rate=SVHN_CLIENTS_PER_ROUND / len(train_ds.client_ids),
        steps=SVHN_ROUNDS,
    )

    client_sampling_rates = get_sampling_rates_per_client(
        budgets_per_client=budgets_per_client, 
        budgets=budgets, 
        sampling_rates_per_budget=qs_per_budget
    )

    def model_fn():
        return _get_model(test_ds.element_spec)

    trained_weights, train_history = train(
        model_fn=model_fn,
        train_data=train_ds,
        test_data=test_ds,
        client_sampling_rates=client_sampling_rates,
        rounds=SVHN_ROUNDS,
        noise_multiplier=noise_multiplier,
        clients_per_round=SVHN_CLIENTS_PER_ROUND,
    )

    Path(save_dir).mkdir(parents=True)
    save_train_results(save_dir, trained_weights, train_history)
