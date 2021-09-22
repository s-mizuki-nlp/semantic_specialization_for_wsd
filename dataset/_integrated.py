#!/usr/bin/env python
# -*- coding:utf-8 -*-
import warnings
from typing import Set, Optional, Dict, Any, Iterator, Union, List

import torch

from .contextualized_embeddings import BERTEmbeddingsDataset
from .lexical_knowledge import LemmaDataset, SynsetDataset

from torch.nn import functional as F
from torch.utils.data import IterableDataset
from .encoder import extract_entity_subword_embeddings, calc_entity_subwords_average_vectors
from . import utils

class WSDTaskDataset(IterableDataset):

    def __init__(self, bert_embeddings_dataset: BERTEmbeddingsDataset,
                 lexical_knowledge_lemma_dataset: LemmaDataset,
                 lexical_knowledge_synset_dataset: Optional[SynsetDataset] = None,
                 n_ancestor_hop_of_ground_truth_synset: int = 0,
                 return_level: str = "entity",
                 record_entity_field_name: str = "monosemous_entities",
                 record_entity_span_field_name: str = "subword_spans",
                 return_entity_subwords_avg_vector: bool = False,
                 raise_error_on_unknown_lemma: bool = True,
                 excludes: Optional[Set[str]] = None):

        self._bert_embeddings = bert_embeddings_dataset
        self._lexical_knowledge_lemma = lexical_knowledge_lemma_dataset
        self._lexical_knowledge_synset = lexical_knowledge_synset_dataset
        self._return_level = return_level
        self._raise_error_on_unknown_lemma = raise_error_on_unknown_lemma
        self._record_entity_field_name = record_entity_field_name
        self._record_entity_span_field_name = record_entity_span_field_name
        self._return_entity_subwords_avg_vector = return_entity_subwords_avg_vector
        self._excludes = set() if excludes is None else excludes

        assert n_ancestor_hop_of_ground_truth_synset >= 0, f"`n_ancestor_hop_of_ground_truth_synset` must be zero or positive: {n_ancestor_hop_of_ground_truth_synset}"
        self._n_ancestor_hop_of_ground_truth_synset = n_ancestor_hop_of_ground_truth_synset
        if self._n_ancestor_hop_of_ground_truth_synset > 0:
            assert lexical_knowledge_synset_dataset, f"you have to specify `lexical_knowledge_synset_dataset` to get ancestor synset code."

    @classmethod
    def extract_entity_spans_from_record(cls, record: Dict[str, Any],
                                         entity_field_name: str,
                                         span_field_name: str):
        lst_entity_spans = [entity[span_field_name] for entity in record[entity_field_name]]
        return lst_entity_spans

    def _test_if_unknown_lemma(self, lemma: str, pos: str) -> bool:
        if (lemma, pos) not in self.lemma_dataset:
            msg = f"unknown lemma detected: ({lemma},{pos})"
            if self._raise_error_on_unknown_lemma:
                raise ValueError(msg)
            else:
                warnings.warn(msg)
                return True

    def _entity_loader(self) -> Iterator[Dict[str, Any]]:
        for obj_sentence in self._sentence_loader():
            lst_entities = obj_sentence["record"][self._record_entity_field_name]
            lst_entity_embeddings = obj_sentence["entity_embeddings"]
            lst_entity_seq_len = obj_sentence["entity_sequence_lengths"]
            lst_entity_span_avg_vectors = obj_sentence.get("entity_span_avg_vectors", [])
            context_embedding = obj_sentence["embedding"]
            context_sequence_length = obj_sentence["sequence_length"]

            for idx, dict_entity_record in enumerate(lst_entities):
                lemma, pos = dict_entity_record["lemma"], dict_entity_record["pos"]
                if self._test_if_unknown_lemma(lemma, pos):
                    continue

                synset_ids = self.lemma_dataset.get_synset_ids(lemma, pos)
                synset_codes = self.lemma_dataset.get_synset_codes(lemma, pos)
                lexnames = self.lemma_dataset[(lemma, pos)]["lexnames"]
                assert (len(synset_ids) == 1) and (len(synset_codes) == 1), \
                    f"specified entity is sense-ambiguous: {','.join(synset_ids)}"

                obj_entity = {
                    "entity_embedding": lst_entity_embeddings[idx],
                    "entity_sequence_length": lst_entity_seq_len[idx],
                    "context_embedding": context_embedding,
                    "context_sequence_length": context_sequence_length,
                    "original_synset_id": synset_ids[0],
                    "original_synset_code": synset_codes[0],
                    "lexname": lexnames[0]
                }

                # (optional) compute average vector of entity spans (subword-level average, then word-level average)
                if self._return_entity_subwords_avg_vector:
                    obj_entity["entity_span_avg_vector"] = lst_entity_span_avg_vectors[idx]

                # (optional) get ancestor synset
                if self._n_ancestor_hop_of_ground_truth_synset == 0:
                    obj_entity["ground_truth_synset_id"] = obj_entity["original_synset_id"]
                    obj_entity["ground_truth_synset_code"] = obj_entity["original_synset_code"]
                else:
                    lst_ancestor_synsets = self.synset_dataset.get_ancestor_synsets(synset_ids[0])
                    if len(lst_ancestor_synsets) == 0:
                        warnings.warn(f"failed to lookup ancestor synset: {synset_ids[0]}")
                        continue
                    idx = min(self._n_ancestor_hop_of_ground_truth_synset, len(lst_ancestor_synsets)) - 1
                    ancestor_synset = lst_ancestor_synsets[idx]
                    obj_entity["ground_truth_synset_id"] = ancestor_synset["id"]
                    obj_entity["ground_truth_synset_code"] = ancestor_synset["code"]

                obj_entity.update(dict_entity_record)

                yield obj_entity

    def _sentence_loader(self) -> Iterator[Dict[str, Any]]:
        """
        returns sentence-level objects.

        returns:
            embedding: sequence of subword embeddings of a sentence. shape: (n_seq_len, n_dim)
            sequence_length: number of subwords in a sentence.
            record: sentence information.
            entity_embeddings: list of the sequence of subword embeddings of the entities. shape: List[(n_window, n_dim)]
            entity_subword_lengths: list of the entity subword window sizes. List[n_window]
        """
        for obj_sentence in self._bert_embeddings:
            record = obj_sentence["record"]
            lst_lst_entity_spans = self.extract_entity_spans_from_record(record,
                                                                         entity_field_name=self._record_entity_field_name,
                                                                         span_field_name=self._record_entity_span_field_name)
            dict_entity_embeddings = extract_entity_subword_embeddings(
                                     context_embeddings=obj_sentence["embedding"],
                                     lst_lst_entity_subword_spans=lst_lst_entity_spans,
                                     padding=False)
            obj_sentence["entity_embeddings"] = dict_entity_embeddings["embeddings"]
            obj_sentence["entity_sequence_lengths"] = dict_entity_embeddings["sequence_lengths"]

            if self._return_entity_subwords_avg_vector:
                obj_sentence["entity_span_avg_vectors"] = calc_entity_subwords_average_vectors(
                                                            context_embeddings=obj_sentence["embedding"],
                                                            lst_lst_entity_subword_spans=lst_lst_entity_spans)

            yield obj_sentence

    def __iter__(self):
        if self._return_level == "entity":
            it_records = self._entity_loader()
        elif self._return_level == "sentence":
            it_records = self._sentence_loader()
        else:
            raise ValueError(f"unknown `return_level` value: {self._return_level}")
        for record in it_records:
            for exclude_field in self._excludes:
                _ = record.pop(exclude_field, None)
            yield record

    def _count_records(self):
        n_records = 0
        for _ in self:
            n_records += 1
        return n_records

    def __len__(self):
        if hasattr(self, "_n_records"):
            return self._n_records
        else:
            self._n_records  = self._count_records()
            return self._n_records

    @property
    def lemma_dataset(self):
        return self._lexical_knowledge_lemma

    @property
    def synset_dataset(self):
        return self._lexical_knowledge_synset

    @property
    def embeddings_dataset(self):
        return self._bert_embeddings

    @property
    def n_ancestor_hop_of_ground_truth_synset(self):
        return self._n_ancestor_hop_of_ground_truth_synset


class WSDTaskDatasetCollateFunction(object):

    def __init__(self,
                 return_records: bool = True,
                 return_entity_context_attn_mask: bool = False,
                 num_heads_entity_context_mha: Optional[int] = None):

        self._return_records = return_records
        self._return_entity_context_attn_mask = return_entity_context_attn_mask

        if return_entity_context_attn_mask:
            assert isinstance(num_heads_entity_context_mha, int), \
                f"you must specify the number of attention heads of MHA module as: `num_heads_entity_context_mha`"
        self._num_heads = num_heads_entity_context_mha

    def __call__(self, lst_entity_objects: List[Dict[str, Any]]):
        def _list_of(field_name: str):
            return [obj[field_name] for obj in lst_entity_objects]

        set_field_names = next(iter(lst_entity_objects)).keys()

        # context embeddings and entity embeddings
        lst_context_sequence_lengths = _list_of("context_sequence_length")
        lst_lagged_context_embeddings = _list_of("context_embedding")
        lst_entity_sequence_lengths = _list_of("entity_sequence_length")
        lst_lagged_entity_span_embeddings = _list_of("entity_embedding")
        dict_ret = {
            "context_sequence_lengths": torch.tensor(lst_context_sequence_lengths),
            "context_embeddings": utils.pad_and_stack_list_of_tensors(lst_lagged_context_embeddings),
            "entity_sequence_lengths": torch.tensor(lst_entity_sequence_lengths),
            "entity_embeddings": utils.pad_and_stack_list_of_tensors(lst_lagged_entity_span_embeddings)
        }
        ## (optional) entity span average vectors
        if "entity_span_avg_vector" in set_field_names:
            dict_ret["entity_span_avg_vectors"] = torch.stack(_list_of("entity_span_avg_vector"))

        # attention masks used for MultiheadAttention and GlobalAttention module.
        _, device = utils.get_dtype_and_device(dict_ret["context_embeddings"])
        dict_ret["entity_sequence_mask"] = utils.create_sequence_mask(lst_entity_sequence_lengths, device=device)
        dict_ret["context_sequence_mask"] = utils.create_sequence_mask(lst_context_sequence_lengths, device=device)

        ## (optional) attn_mask for MultiheadAttention module.
        if self._return_entity_context_attn_mask:
            entity_context_attn_mask = utils.create_multiheadattention_attn_mask_batch(
                lst_query_sequence_lengths=lst_entity_sequence_lengths,
                lst_key_value_sequence_lengths=lst_context_sequence_lengths,
                target_sequence_length=max(lst_entity_sequence_lengths),
                source_sequence_length=max(lst_context_sequence_lengths),
                num_heads=self._num_heads,
                device=device
            )
            dict_ret["entity_context_attn_mask"] = entity_context_attn_mask

        # ground truth: synset code
        dict_ret["ground_truth_synset_codes"] = torch.tensor(_list_of("ground_truth_synset_code"), dtype=torch.long, device=device)
        dict_ret["ground_truth_synset_ids"] = _list_of("ground_truth_synset_id")

        # other attributes are accumulated as `records` object.
        if self._return_records:
            trim_plural = lambda name: name[:-1] if name.endswith("s") else name
            set_uncaught_fields = set_field_names - set([trim_plural(name) for name in dict_ret.keys()])
            lst_records = [{name:e_object.get(name, None) for name in set_uncaught_fields} for e_object in lst_entity_objects]
            dict_ret["records"] = lst_records

        return dict_ret