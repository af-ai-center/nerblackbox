
import os
from ner.utils.env_variable import ENV_VARIABLE

from os.path import abspath, dirname, join
BASE_DIR = abspath(dirname(dirname(dirname(__file__))))

from argparse import Namespace


def get_available_datasets():
    """
    get datasets that are available in DIR_DATASETS directory
    ---------------------------------------------------------
    :return: available datasets: [list] of [str], e.g. ['suc', 'swedish_ner_corpus']
    """
    dir_datasets = ENV_VARIABLE['DIR_DATASETS']
    return [
        folder
        for folder in os.listdir(dir_datasets)
        if os.path.isdir(join(dir_datasets, folder))
    ]


def get_dataset_path(dataset):
    """
    get dataset path for dataset
    ----------------------------
    :param dataset:        [str] dataset name, e.g. 'suc', 'swedish_ner_corpus'
    :return: dataset_path: [str] path to dataset directory
    """
    dir_datasets = ENV_VARIABLE['DIR_DATASETS']

    if dataset in ['suc', 'swedish_ner_corpus', 'conll2003']:
        dataset_path = join(BASE_DIR, dir_datasets, dataset)
    else:
        raise Exception(f'dataset = {dataset} unknown.')

    return dataset_path


def get_hardcoded_parameters(keys=False):
    """
    :param keys: [bool] whether to return [list] of keys instead of whole [dict]
    :return: _general:  [dict] w/ keys = parameter name [str] & values = type [str] --- or [list] of keys
    :return: _params:   [dict] w/ keys = parameter name [str] & values = type [str] --- or [list] of keys
    :return: _hparams:  [dict] w/ keys = parameter name [str] & values = type [str] --- or [list] of keys
    :return: _log_dirs: [dict] w/ keys = parameter name [str] & values = type [str] --- or [list] of keys
    """
    _general = {
        'experiment_name': 'str',
        'run_name': 'str',
        'device': 'str',
        'fp16': 'bool',
        'experiment_run_name': 'str',
    }
    _params = {
        'dataset_name': 'str',
        'dataset_tags': 'str',
        'prune_ratio_train': 'float',
        'prune_ratio_val': 'float',
        'prune_ratio_test': 'float',
        'pretrained_model_name': 'str',
        'uncased': 'bool',
        'checkpoints': 'bool',
        'logging_level': 'str',
    }
    _hparams = {
        'batch_size': 'int',
        'max_seq_length': 'int',
        'max_epochs': 'int',
        'monitor': 'str',
        'min_delta': 'float',
        'patience': 'int',
        'mode': 'str',
        'lr_max': 'float',
        'lr_schedule': 'str',
        'lr_warmup_epochs': 'int',
        'lr_num_cycles': 'int',
    }
    _log_dirs = {
        'mlflow': 'str',
        'tensorboard': 'str',
        'checkpoints': 'str',
        'log_file': 'str',
        'mlflow_file': 'str',
    }
    if keys:
        return list(_general.keys()), list(_params.keys()), list(_hparams.keys()), list(_log_dirs.keys())
    else:
        return _general, _params, _hparams, _log_dirs


def unify_parameters(_params, _hparams, _log_dirs, _experiment):
    """
    unify parameters (namespaces, bool) to one namespace
    ----------------------------------------------------
    :param _params:             [Namespace] with keys = 'dataset_name', 'dataset_tags', ..
    :param _hparams:            [Namespace] with keys = 'batch_size', 'max_seq_length', ..
    :param _log_dirs:           [Namespace] with keys = 'mlflow', 'tensorboard', ..
    :param _experiment:         [bool]
    :return: _lightning_hparams [Namespace] with keys = all keys from input namespaces + 'experiment'
    """
    _dict = vars(_params)
    _dict.update(vars(_hparams))
    _dict.update(vars(_log_dirs))
    _dict.update({'experiment': _experiment})
    _lightning_hparams = Namespace(**_dict)
    _lightning_hparams.device = _lightning_hparams.device.type  # needs to be a string (not torch.device) for logging
    return _lightning_hparams


def split_parameters(_lightning_hparams):
    """
    split namespace to parameters (namespaces, bool)
    ----------------------------------------------------
    :param _lightning_hparams [Namespace] with keys = all keys from output namespaces + 'experiment'
    :return: _params:         [Namespace] with keys = 'dataset_name', 'dataset_tags', ..
    :return: _hparams:        [Namespace] with keys = 'batch_size', 'max_seq_length', ..
    :return: _log_dirs:       [Namespace] with keys = 'mlflow', 'tensorboard', ..
    :return: _experiment:     [bool]
    """
    keys_general, keys_params, keys_hparams, keys_log_dirs = get_hardcoded_parameters(keys=True)
    _params = Namespace(**{k: v for k, v in vars(_lightning_hparams).items() if k in keys_general + keys_params})
    _hparams = Namespace(**{k: v for k, v in vars(_lightning_hparams).items() if k in keys_hparams})
    _log_dirs = Namespace(**{k: v for k, v in vars(_lightning_hparams).items() if k in keys_log_dirs})
    _experiment = vars(_lightning_hparams).get('experiment')
    return _params, _hparams, _log_dirs, _experiment