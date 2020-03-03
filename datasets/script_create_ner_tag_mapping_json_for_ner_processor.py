
import argparse
import json

from os.path import abspath, dirname
import sys
BASE_DIR = abspath(dirname(dirname(__file__)))
sys.path.append(BASE_DIR)

from datasets.formatter.swedish_ner_corpus_formatter import SwedishNerCorpusFormatter
from datasets.formatter.suc_formatter import SUCFormatter


def main(args):
    """
    writes ner_tag_mapping.json file
    ----------------------------------
    :param args: [argparse parsed arguments]
        ner_dataset: [str], e.g. 'swedish_ner_corpus'
        with_tags:   [bool]. If true, have tags like 'B-PER', 'I-PER'. If false, have tags like 'PER'.
        modify:      [bool], if True: modify tags as specified in method modify_ner_tag_mapping()
    :return: -
    """
    # formatter
    if args.ner_dataset == 'swedish_ner_corpus':
        formatter = SwedishNerCorpusFormatter()
    elif args.ner_dataset == 'SUC':
        formatter = SUCFormatter()
    else:
        raise Exception(f'ner_dataset = {args.ner_dataset} unknown.')

    # ner tag mapping
    ner_tag_mapping = formatter.create_ner_tag_mapping(with_tags=args.with_tags, modify=args.modify)

    json_path = f'datasets/ner/{args.ner_dataset}/ner_tag_mapping.json'
    with open(json_path, 'w') as f:
        json.dump(ner_tag_mapping, f)

    print(f'> dumped the following dict to {json_path}:')
    print(ner_tag_mapping)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ner_dataset', required=True, type=str, help='e.g. swedish_ner_corpus')
    parser.add_argument('--with_tags', action='store_true')
    parser.add_argument('--modify', action='store_true')
    _args = parser.parse_args()

    main(_args)
