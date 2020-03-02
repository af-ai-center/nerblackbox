
import os
import time
import torch
import pickle
import numpy as np
from tensorboardX import SummaryWriter
from seqeval.metrics import classification_report as classification_report_seqeval
from sklearn.metrics import classification_report as classification_report_sklearn

from apex import amp
# from torch.optim import Adam
from transformers import AdamW
from transformers import get_linear_schedule_with_warmup
from transformers import get_constant_schedule_with_warmup
from transformers import get_cosine_schedule_with_warmup
from transformers import get_cosine_with_hard_restarts_schedule_with_warmup
from tqdm import tqdm_notebook as tqdm

from utils import utils
from utils.env_variable import ENV_VARIABLE
from utils.mlflow_client import MLflowClient
from utils.ner_metrics import NerMetrics
from utils.logged_metrics import LoggedMetrics


class NERTrainer:
    """
    Trainer of BERT model for NER downstream task
    """
    def __init__(self,
                 model,
                 device,
                 train_dataloader,
                 valid_dataloader,
                 label_list,
                 hyperparams=None,
                 fp16=False,
                 verbose=False):
        """
        :param model:            [transformers BertForTokenClassification]
        :param device:           [torch device] 'cuda' or 'cpu'
        :param train_dataloader: [pytorch DataLoader]
        :param valid_dataloader: [pytorch DataLoader]
        :param label_list:       [list] of [str], e.g. ['[PAD]', '[CLS]', '[SEP]', 'O', 'PER', ..]
        :param hyperparams:      [dict] for mlflow tracking
        :param fp16:             [bool]
        :param verbose:          [bool] verbose print
        """
        # input attributes
        self.model = model
        self.train_dataloader = train_dataloader
        self.valid_dataloader = valid_dataloader
        self.label_list = label_list
        self.device = device
        self.fp16 = fp16
        self.verbose = verbose

        # device
        self.model.to(self.device)
        if fp16:
            self.model.half()

        # tensorboard & mlflow
        logged_metrics = [
            ('loss', ['all'], ['simple']),
            ('acc', ['all'], ['simple']),
            ('precision', ['all', 'fil'], ['micro', 'macro']),
            ('precision', ['ind'], ['micro']),
            ('recall', ['all', 'fil'], ['micro', 'macro']),
            ('recall', ['ind'], ['micro']),
            ('f1', ['all', 'fil'], ['micro', 'macro']),
            ('f1', ['ind'], ['micro']),
        ]
        self.logged_metrics = LoggedMetrics(logged_metrics)
        tensorboard_dir = os.path.join(ENV_VARIABLE['DIR_TENSORBOARD'],
                                       os.environ['MLFLOW_EXPERIMENT_NAME'],
                                       os.environ['MLFLOW_RUN_NAME'])
        self.writer = SummaryWriter(log_dir=tensorboard_dir)
        self.mlflow_client = MLflowClient(log_dir=ENV_VARIABLE['DIR_MLFLOW'],
                                          logged_metrics=self.logged_metrics.as_flat_list())
        self.mlflow_client.log_params(hyperparams)

        # no input arguments (set in methods)
        self.optimizer = None
        self.scheduler = None
        self.pbar = None
        self.max_grad_norm = None

        # metrics
        self.metrics = self.instantiate_metrics_dict()

    def get_filtered_labels(self):
        return [label
                for label in self.label_list
                if not (label.startswith('[') or label == 'O')]

    def get_filtered_label_ids(self):
        return [self.label_list.index(label)
                for label in self.label_list
                if not (label.startswith('[') or label == 'O')]

    def get_individual_label_id(self, label):
        return [self.label_list.index(label)]

    ####################################################################################################################
    # 1. FIT & VALIDATION
    ####################################################################################################################
    def fit(self,
            num_epochs=25,
            max_grad_norm=None,  # 2.0
            lr_max=3e-5,
            lr_schedule='linear',
            lr_warmup_fraction=0.1,
            lr_num_cycles=None):
        """
        train & validate
        ----------------
        :param num_epochs:          [int]
        :param max_grad_norm:       [float]
        :param lr_max:              [float] basic learning rate
        :param lr_schedule:         [str], 'linear', 'constant', 'cosine', 'cosine_with_hard_resets'
        :param lr_warmup_fraction:  [float], e.g. 0.1, fraction of steps during which lr is warmed up
        :param lr_num_cycles:       [float, optional], e.g. 0.5, 1.0, only for cosine learning rate schedules
        :return: -
        """
        self.max_grad_norm = max_grad_norm

        # optimizer
        self.optimizer = self.create_optimizer(lr_max, self.fp16)
        if self.device == 'cpu':
            self.model, self.optimizer = amp.initialize(self.model, self.optimizer, opt_level='O1')

        # learning rate
        self.scheduler = self.create_scheduler(num_epochs, lr_schedule, lr_warmup_fraction, lr_num_cycles)

        ################################################################################################################
        # start training
        ################################################################################################################
        self.pbar = tqdm(total=num_epochs*len(self.train_dataloader))

        start = time.time()
        for epoch in range(num_epochs):
            print('\n>>> Epoch: {}'.format(epoch))
            self.train(epoch)
            self.validate(epoch)
        end = time.time()
        print(f'> train & validate time on device = {self.device} for {num_epochs} epochs: {end - start:.2f}s')

        # mlflow & tensorboard
        self.mlflow_client.log_time(end - start)
        self.mlflow_client.finish()
        self.writer.close()

    def train(self, epoch):
        """
        train one epoch
        ---------------
        :param epoch: [int]
        :return: -
        """
        print('> Train')
        self.model.train()

        for batch_train_step, batch in enumerate(self.train_dataloader):
            self.model.zero_grad()

            # get batch data
            batch = tuple(t.to(self.device) for t in batch)
            input_ids, input_mask, segment_ids, label_ids = batch

            # forward pass
            outputs = self.model(input_ids,
                                 attention_mask=input_mask,
                                 token_type_ids=segment_ids,
                                 labels=label_ids,
                                 )
            batch_train_loss, logits = outputs[:2]

            # to cpu/numpy
            np_batch_train = {
                'loss': batch_train_loss.detach().cpu().numpy(),
                'label_ids': label_ids.to('cpu').numpy(),    # shape: [batch_size, seq_legnth]
                'logits': logits.detach().cpu().numpy(),     # shape: [batch_size, seq_length, num_labels]
            }

            # batch train metrics
            batch_train_metrics, _, progress_bar = self.compute_metrics('batch', 'train', np_batch_train)

            # backpropagation
            if self.fp16:
                with amp.scale_loss(batch_train_loss, self.optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                batch_train_loss.backward()

            if self.max_grad_norm is not None:
                # TODO: undersök varför man vill göra det här, det får ibland modellen att inte lära sig
                self.clip_grad_norm()
            self.optimizer.step()

            # update learning rate (after optimization step!)
            lr_this_step = self.scheduler.get_lr()[0]
            self.metrics['batch']['train']['lr'].append(lr_this_step)
            self.scheduler.step()

            # output
            global_step = self.get_global_step(epoch, batch_train_step)
            self.write_metrics_for_tensorboard('train', batch_train_metrics, global_step, lr_this_step)

            self.pbar.update(1)  # TQDM - training progressbar
            self.pbar.set_description(progress_bar)

            # batch loss
            if self.verbose:
                print(f'Batch #{batch_train_step} train loss:    {batch_train_loss:.2f}')
                print(f'                          learning rate: {lr_this_step:.2e}')

    def validate(self, epoch):
        """
        validation after each training epoch
        ------------------------------------
        :param epoch: [int]
        :return: -
        """
        print('\n> Validate')
        self.model.eval()

        valid_fields = ['loss', 'label_ids', 'logits']
        np_epoch_valid = {valid_field: None for valid_field in valid_fields}
        np_epoch_valid_list = {valid_field: list() for valid_field in valid_fields}

        for batch in self.valid_dataloader:
            batch = tuple(t.to(self.device) for t in batch)
            input_ids, input_mask, segment_ids, label_ids = batch
            
            with torch.no_grad():
                outputs = self.model(input_ids,
                                     attention_mask=input_mask,
                                     token_type_ids=segment_ids,
                                     labels=label_ids)
                batch_valid_loss, logits = outputs[:2]

            # to cpu/numpy
            np_batch_valid = {
                'loss': batch_valid_loss.detach().cpu().numpy(),
                'label_ids': label_ids.to('cpu').numpy(),
                'logits': logits.detach().cpu().numpy(),
            }

            # epoch metrics
            for valid_field in valid_fields:
                np_epoch_valid_list[valid_field].append(np_batch_valid[valid_field])

        # epoch metrics
        np_epoch_valid['loss'] = np.mean(np_epoch_valid_list['loss'])
        np_epoch_valid['label_ids'] = np.vstack(np_epoch_valid_list['label_ids'])
        np_epoch_valid['logits'] = np.vstack(np_epoch_valid_list['logits'])

        epoch_valid_metrics, epoch_valid_label_ids, _ = self.compute_metrics('epoch', 'valid', np_epoch_valid)

        # output
        self.print_metrics(epoch, epoch_valid_metrics)
        global_step = self.get_global_step(epoch, len(self.train_dataloader)-1)
        self.write_metrics_for_tensorboard('valid', epoch_valid_metrics, global_step)
        self.mlflow_client.log_metrics(epoch, epoch_valid_metrics)
        self.print_classification_reports(epoch, epoch_valid_label_ids)

    ####################################################################################################################
    # 2. METRICS
    ####################################################################################################################
    @staticmethod
    def instantiate_metrics_dict():
        metrics = dict()
        for elem1 in ['batch', 'epoch']:
            metrics[elem1] = dict()
            for elem2 in ['train', 'valid']:
                metrics[elem1][elem2] = dict()
                for elem3 in ['all', 'fil']:
                    metrics[elem1][elem2][elem3] = dict()
                    for elem4 in ['f1']:
                        metrics[elem1][elem2][elem3][elem4] = dict()
                        for elem5 in ['macro', 'micro']:
                            metrics[elem1][elem2][elem3][elem4][elem5] = list()
                for elem4 in ['loss', 'acc']:
                    metrics[elem1][elem2]['all'][elem4] = list()
        metrics['batch']['train']['lr'] = list()

        return metrics

    def compute_metrics_for_specific_tags(self, _label_ids, tags: str):
        """
        compute metrics for specific tags (e.g. 'all', 'fil')
        -----------------------------------------------------
        :param _label_ids: [dict] w/ keys 'true', 'pred'      & values = [np array]
        :param tags:       [str], e.g. 'all', 'fil'
        :return: _metrics  [dict] w/ keys = metric (e.g. 'all_precision_micro') and value = [float]
        """
        if tags == 'all':
            labels = None
            logged_metrics_tags = ['all']
        elif tags == 'fil':
            labels = self.get_filtered_label_ids()
            logged_metrics_tags = ['fil']
        else:
            labels = self.get_individual_label_id(tags)
            logged_metrics_tags = ['ind']

        _metrics = dict()
        ner_metrics = NerMetrics(_label_ids['true'], _label_ids['pred'], labels=labels)
        ner_metrics.compute(self.logged_metrics.get_metrics(tags=logged_metrics_tags))
        results = ner_metrics.results_as_dict()

        for metric_type in self.logged_metrics.get_metrics(tags=logged_metrics_tags, micro_macros=['simple'], exclude=['loss']):
            if results[metric_type] is not None:
                _metrics[f'{tags}_{metric_type}'] = results[metric_type]
        for metric_type in self.logged_metrics.get_metrics(tags=logged_metrics_tags, micro_macros=['micro']):
            if results[f'{metric_type}_micro'] is not None:
                if logged_metrics_tags == ['ind']:
                    _metrics[f'{tags}_{metric_type}'] = results[f'{metric_type}_micro']
                else:
                    _metrics[f'{tags}_{metric_type}_micro'] = results[f'{metric_type}_micro']
        for metric_type in self.logged_metrics.get_metrics(tags=logged_metrics_tags, micro_macros=['macro']):
            if results[f'{metric_type}_macro'] is not None:
                _metrics[f'{tags}_{metric_type}_macro'] = results[f'{metric_type}_macro']

        return _metrics

    def compute_metrics(self, size, phase, _np_dict):
        """
        computes loss, acc, f1 scores for size/phase = batch/train or epoch/valid
        -------------------------------------------------------------------------
        :param size:           [str] 'batch' or 'epoch'
        :param phase:          [str] 'train' or 'valid'
        :param _np_dict:       [dict] w/ key-value pairs:
                                     'loss':       [np value]
                                     'label_ids':  [np array] of shape [batch_size, seq_length]
                                     'logits'      [np array] of shape [batch_size, seq_length, num_labels]
        :return: metrics       [dict] w/ keys 'loss', 'acc', 'f1' & values = [np array]
                 labels_ids    [dict] w/ keys 'true', 'pred'      & values = [np array]
                 _progress_bar [str] to display during training
        """
        # batch / dataset
        label_ids = dict()
        label_ids['true'], label_ids['pred'] = self.reduce_and_flatten(_np_dict['label_ids'], _np_dict['logits'])

        # batch / dataset metrics
        metrics = {'all_loss': _np_dict['loss']}
        metrics.update(self.compute_metrics_for_specific_tags(label_ids, tags='all'))
        metrics.update(self.compute_metrics_for_specific_tags(label_ids, tags='fil'))
        for label in self.get_filtered_labels():
            metrics.update(self.compute_metrics_for_specific_tags(label_ids, tags=label))

        """
        # append to self.metrics
        self.metrics[size][phase]['all']['loss'].append(metrics['all_loss'])
        self.metrics[size][phase]['all']['acc'].append(metrics['all_acc'])
        self.metrics[size][phase]['all']['f1']['macro'].append(metrics['all_f1_macro'])
        self.metrics[size][phase]['all']['f1']['micro'].append(metrics['all_f1_micro'])
        self.metrics[size][phase]['fil']['f1']['macro'].append(metrics['fil_f1_macro'])
        self.metrics[size][phase]['fil']['f1']['micro'].append(metrics['fil_f1_micro'])
        """

        # progress bar
        _progress_bar = 'all acc: {:.2f} | fil f1 (macro): {:.2f} | fil f1 (micro): {:.2f}'.format(
            metrics['all_acc'],
            metrics['fil_f1_macro'],
            metrics['fil_f1_micro'],
        )

        return metrics, label_ids, _progress_bar

    @staticmethod
    def reduce_and_flatten(_np_label_ids, _np_logits):
        """
        reduce _np_logits (3D -> 2D), flatten both np arrays (2D -> 1D)
        ---------------------------------------------------------------
        :param _np_label_ids: [np array] of shape [batch_size, seq_length]
        :param _np_logits:    [np array] of shape [batch_size, seq_length, num_labels]
        :return: true_flat:   [np array] of shape [batch_size * seq_length], _np_label_ids             flattened
                 pred_flat:   [np array] of shape [batch_size * seq_length], _np_logits    reduced and flattened
        """
        true_flat = _np_label_ids.flatten()
        pred_flat = np.argmax(_np_logits, axis=2).flatten()
        return true_flat, pred_flat

    ####################################################################################################################
    # 2b. METRICS LOGGING
    ####################################################################################################################
    @staticmethod
    def print_metrics(epoch, epoch_valid_metrics):
        print('Epoch #{} valid all loss:         {:.2f}'.format(epoch, epoch_valid_metrics['all_loss']))
        print('Epoch #{} valid all acc:          {:.2f}'.format(epoch, epoch_valid_metrics['all_acc']))
        print('Epoch #{} valid all f1 (macro):   {:.2f}'.format(epoch, epoch_valid_metrics['all_f1_macro']))
        print('Epoch #{} valid all f1 (micro):   {:.2f}'.format(epoch, epoch_valid_metrics['all_f1_micro']))
        print('Epoch #{} valid fil f1 (macro):   {:.2f}'.format(epoch, epoch_valid_metrics['fil_f1_macro']))
        print('Epoch #{} valid fil f1 (micro):   {:.2f}'.format(epoch, epoch_valid_metrics['fil_f1_micro']))

    def write_metrics_for_tensorboard(self, phase, metrics, _global_step, _lr_this_step=None):
        """
        write metrics for tensorboard
        -----------------------------
        :param phase:         [str] 'train' or 'valid'
        :param metrics:       [dict] w/ keys 'loss', 'acc', 'f1_macro_all', 'f1_micro_all'
        :param _global_step:  [int]
        :param _lr_this_step: [float] only needed if phase == 'train'
        :return: -
        """
        for key in metrics.keys():
            self.writer.add_scalar(f'{phase}/{key}', metrics[key], _global_step)

        if phase == 'train' and _lr_this_step is not None:
            self.writer.add_scalar(f'{phase}/learning_rate', _lr_this_step, _global_step)

    def print_classification_reports(self, epoch, epoch_valid_label_ids):
        """
        print token-based (sklearn) & chunk-based (seqeval) classification reports
        --------------------------------------------------------------------------
        :param: epoch:                 [int]
        :param: epoch_valid_label_ids: [dict] w/ keys 'true', 'pred'      & values = [np array]
        :return: -
        """
        self.mlflow_client.clear_artifact()

        # use labels instead of label_ids
        epoch_valid_labels = {
            field: [self.label_list[label_id] for label_id in epoch_valid_label_ids[field]]
            for field in ['true', 'pred']
        }

        # token-based classification report
        self.mlflow_client.log_artifact(f'\n>>> Epoch: {epoch}')
        self.mlflow_client.log_artifact('\n--- token-based (sklearn) classification report ---')
        selected_labels = [label for label in self.label_list if label != 'O' and not label.startswith('[')]
        self.mlflow_client.log_artifact(classification_report_sklearn(epoch_valid_labels['true'],
                                                                      epoch_valid_labels['pred'],
                                                                      labels=selected_labels))

        # enrich pred_tags & valid_tags with bio prefixes
        epoch_valid_labels_bio = {
            field: utils.add_bio_to_label_list(utils.get_rid_of_special_tokens(epoch_valid_labels[field]))
            for field in ['true', 'pred']
        }

        # chunk-based classification report
        self.mlflow_client.log_artifact('\n--- chunk-based (seqeval) classification report ---')
        self.mlflow_client.log_artifact(classification_report_seqeval(epoch_valid_labels_bio['true'],
                                                                      epoch_valid_labels_bio['pred'],
                                                                      suffix=False))

    ####################################################################################################################
    # 3. STEPS
    ####################################################################################################################
    def get_global_step(self, _epoch, _batch_train_step):
        """
        get global training step
        ------------------------
        :param _epoch:            [int] >= 0
        :param _batch_train_step: [int] >= 0
        :return: global step:     [int]
        """
        return _epoch * len(self.train_dataloader) + _batch_train_step
    
    def get_total_steps(self, _num_epochs):
        """
        gets total_steps = num_epochs * (number of training data samples)
        -----------------------------------------------------------------
        :param _num_epochs:    [int], e.g. 10
        :return: total_steps: [int], e.g. 2500 (in case of 250 training data samples)
        """
        return _num_epochs * len(self.train_dataloader)

    ####################################################################################################################
    # 4. OPTIMIZER
    ####################################################################################################################
    def clip_grad_norm(self):
        torch.nn.utils.clip_grad_norm_(parameters=self.model.parameters(), max_norm=self.max_grad_norm)

    def create_optimizer(self, learning_rate, fp16=True, no_decay=('bias', 'gamma', 'beta')):
        """
        create optimizer with basic learning rate and L2 normalization for some parameters
        ----------------------------------------------------------------------------------
        :param learning_rate: [float] basic learning rate
        :param fp16:          [bool]
        :param no_decay:      [tuple of str] parameters that contain one of those are not subject to L2 normalization
        :return: optimizer:   [pytorch optimizer]
        """
        # Remove unused pooler that otherwise break Apex
        param_optimizer = list(self.model.named_parameters())
        param_optimizer = [n for n in param_optimizer if 'pooler' not in n[0]]
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.02},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        # print('> param_optimizer')
        # print([n for n, p in param_optimizer])
        print('> {} parameters w/  weight decay'.format(len(optimizer_grouped_parameters[0]['params'])))
        print('> {} parameters w/o weight decay'.format(len(optimizer_grouped_parameters[1]['params'])))
        if fp16:
            optimizer = AdamW(optimizer_grouped_parameters, lr=learning_rate)
            # optimizer = FusedAdam(optimizer_grouped_parameters, lr=self.learning_rate, bias_correction=False)
            # optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
            
        else:
            optimizer = AdamW(optimizer_grouped_parameters, lr=learning_rate)
            # optimizer = FusedAdam(optimizer_grouped_parameters, lr=learning_rate)

        # optimizer = BertAdam(optimizer_grouped_parameters,lr=2e-5, warmup=.1)
        return optimizer

    def create_scheduler(self, _num_epochs, _lr_schedule, _lr_warmup_fraction, _lr_num_cycles=None):
        """
        create scheduler with warmup
        ----------------------------
        :param _num_epochs:         [int]
        :param _lr_schedule:        [str], 'linear', 'constant', 'cosine', 'cosine_with_hard_resets'
        :param _lr_warmup_fraction: [float], e.g. 0.1, fraction of steps during which lr is warmed up
        :param _lr_num_cycles:      [float, optional], e.g. 0.5, 1.0, only for cosine learning rate schedules
        :return: scheduler          [torch LambdaLR] learning rate scheduler
        """
        if _lr_schedule not in ['constant', 'linear', 'cosine', 'cosine_with_hard_restarts']:
            raise Exception(f'lr_schedule = {_lr_schedule} not implemented.')

        total_steps = self.get_total_steps(_num_epochs)

        scheduler_params = {
            'num_warmup_steps': int(_lr_warmup_fraction * total_steps),
            'last_epoch': -1,
        }

        if _lr_schedule == 'constant':
            return get_constant_schedule_with_warmup(self.optimizer, **scheduler_params)
        else:
            scheduler_params['num_training_steps'] = total_steps

            if _lr_schedule == 'linear':
                return get_linear_schedule_with_warmup(self.optimizer, **scheduler_params)
            else:
                if _lr_num_cycles is not None:
                    scheduler_params['num_cycles'] = _lr_num_cycles  # else: use default values

                if _lr_schedule == 'cosine':
                    scheduler_params['num_training_steps'] = total_steps
                    return get_cosine_schedule_with_warmup(self.optimizer, **scheduler_params)
                elif _lr_schedule == 'cosine_with_hard_restarts':
                    scheduler_params['num_training_steps'] = total_steps
                    return get_cosine_with_hard_restarts_schedule_with_warmup(self.optimizer, **scheduler_params)
                else:
                    raise Exception('create scheduler: logic is broken.')  # this should never happen

    ####################################################################################################################
    # 5. SAVE MODEL & METRICS
    ####################################################################################################################
    def save_model_checkpoint(self, dataset, pretrained_model_name, num_epochs, prune_ratio, lr_schedule):
        dir_checkpoints = ENV_VARIABLE['DIR_CHECKPOINTS']

        model_name = pretrained_model_name.split('/')[-1]
        pkl_path = f'{dir_checkpoints}/saved__{dataset}__{model_name}__{num_epochs}__{prune_ratio}__{lr_schedule}.pkl'

        torch.save(self.model.state_dict(), pkl_path)
        print(f'checkpoint saved at {pkl_path}')

    def save_metrics(self, dataset, pretrained_model_name, num_epochs, prune_ratio, lr_schedule):
        dir_checkpoints = ENV_VARIABLE['DIR_CHECKPOINTS']

        model_name = pretrained_model_name.split('/')[-1]
        pkl_path = f'{dir_checkpoints}/metrics__{dataset}__{model_name}__{num_epochs}__{prune_ratio}__{lr_schedule}.pkl'

        with open(pkl_path, 'wb') as f:
            pickle.dump(self.metrics, f)
        print(f'metrics saved at {pkl_path}')
