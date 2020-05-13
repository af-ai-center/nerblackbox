
import argparse
import torch
import mlflow

import logging
import warnings

import ner_black_box.ner_training.bert_ner_single as bert_ner_single
from ner_black_box.utils.env_variable import env_variable
from ner_black_box.experiment_config.experiment_config import ExperimentConfig

logging.basicConfig(level=logging.WARNING)  # basic setting that is mainly applied to mlflow's default logging
warnings.filterwarnings('ignore')


def main(params, log_dirs):
    """
    :param params:   [argparse.Namespace] attr: experiment_name, run_name, device, fp16, experiment_run_name
    :param log_dirs: [argparse.Namespace] attr: mlflow, tensorboard
    :return: -
    """
    experiment_config = ExperimentConfig(experiment_name=params.experiment_name,
                                         run_name=params.run_name,
                                         device=params.device,
                                         fp16=params.fp16)
    runs, runs_params, runs_hparams = experiment_config.parse()

    with mlflow.start_run(run_name=params.experiment_name):
        for k, v in experiment_config.get_params_and_hparams(run_name=None).items():
            mlflow.log_param(k, v)

        for run in runs:
            # params & hparams: dict -> namespace
            params = argparse.Namespace(**runs_params[run])
            hparams = argparse.Namespace(**runs_hparams[run])

            # bert_ner: single run
            bert_ner_single.main(params, hparams, log_dirs, experiment=True)


def _parse_args(_parser, _args):
    """
    :param _parser: [argparse ArgumentParser]
    :param _args:   [argparse arguments]
    :return _params:   [argparse.Namespace] attr: experiment_name, run_name, device, fp16, experiment_run_name
    :return _log_dirs: [argparse.Namespace] attr: mlflow, tensorboard
    """
    # parsing
    _params = None
    for group in _parser._action_groups:
        group_dict = {a.dest: getattr(_args, a.dest, None) for a in group._group_actions}
        if group.title == 'args_general':
            group_dict['device'] = torch.device(
                'cuda' if torch.cuda.is_available() and group_dict['device'] == 'gpu' else 'cpu')
            group_dict['fp16'] = True if group_dict['fp16'] and group_dict['device'].type == 'cuda' else False
            if len(group_dict['run_name']) == 0:
                group_dict['run_name'] = None
            _params = argparse.Namespace(**group_dict)

    # log_dirs
    _log_dirs_dict = {
        'mlflow': env_variable('DIR_MLFLOW'),
        'tensorboard': env_variable('DIR_TENSORBOARD'),
        'checkpoints': env_variable('DIR_CHECKPOINTS'),
        'log_file': env_variable('LOG_FILE'),
        'mlflow_file': env_variable('MLFLOW_FILE'),
    }
    _log_dirs = argparse.Namespace(**_log_dirs_dict)

    return _params, _log_dirs


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # params
    args_general = parser.add_argument_group('args_general')
    args_general.add_argument('--experiment_name', type=str, required=True)  # .. logging w/ mlflow & tensorboard
    args_general.add_argument('--run_name', type=str, required=True)         # .. logging w/ mlflow & tensorboard
    args_general.add_argument('--device', type=str, required=True)           # .. device
    args_general.add_argument('--fp16', type=bool, required=True)            # .. device

    args = parser.parse_args()
    _params, _log_dirs = _parse_args(parser, args)
    main(_params, _log_dirs)