#!/usr/bin/env python
# -*- coding:utf-8 -*-

from typing import Optional, Tuple, Union, Iterable, Set, List
import os, sys, io
from collections import defaultdict
import warnings
import torch
import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
from nltk.corpus import wordnet as wn

from dataset.gloss_embeddings import SREFLemmaEmbeddingsDataset
from dataset.utils import tensor_to_numpy, numpy_to_tensor, batch_tile
from dataset.sense_expansion import gloss_extend, get_lexname_synsets
from dataset_preprocessor import utils_wordnet_gloss


class TryAgainMechanism(object):

    def __init__(self,
                 lemma_key_embeddings_dataset: SREFLemmaEmbeddingsDataset,
                 exclude_common_semantically_related_synsets: bool = True,
                 lookup_first_lemma_sense_only: bool = True,
                 average_similarity_in_synset: bool = False,
                 exclude_oneselves_for_noun_and_verb: bool = True,
                 do_not_fix_synset_degeneration_bug: bool = True,
                 semantic_relation: str = 'all-relations',
                 similarity_metric: str = "cosine",
                 device: Optional[str] = "cpu",
                 verbose: bool = False):
        """
        Try-again Mechanism algorithm that is proposed in [Wang and Wang, EMNLP2020].
        Reference: https://github.com/lwmlyy/SREF
        There are a number of rules that are unwritten in the paper, thus we used the algorithm proposed in [Wang and Wang, ACL2021] instead.

        Args:
            lemma_key_embeddings_dataset: Sense embeddings dataset.
            exclude_common_semantically_related_synsets: remove semantically related synsets that are shared among candidate senses.
            lookup_first_lemma_sense_only: use first lemma key when we calculate similarity between candidate lemma key and the synset in the try-again synsets.
            average_similarity_in_synset: take average among lemma keys when we calculate similarity between candidate lemma key and the synset.
            exclude_oneselves_for_noun_and_verb: exclude oneself from the try again synsets for NOUN and VERB.
            do_not_fix_synset_degeneration_bug: return least similar lemma key when all candidate lemma keys belong to the identical synset.
            semantic_relation: semantic relations used for finding try again synsets.
        """
        self._lemma_key_embeddings_dataset = lemma_key_embeddings_dataset

        if similarity_metric not in ("cosine","dot"):
            raise ValueError(f"invalid `similarity_module` name: {similarity_metric}")
        self._similarity_metric = similarity_metric

        self._exclude_common_semantically_related_synsets = exclude_common_semantically_related_synsets
        self._lookup_first_lemma_sense_only = lookup_first_lemma_sense_only
        self._average_similarity_in_synset = average_similarity_in_synset
        self._exclude_oneselves_for_noun_and_verb = exclude_oneselves_for_noun_and_verb
        self._do_not_fix_synset_degeneration_bug = do_not_fix_synset_degeneration_bug
        self._semantic_relation = semantic_relation

        self.verbose = verbose
        self._device = device

    def try_again_mechanism(self,
                            vec_query_embedding: torch.Tensor,
                            pos: str,
                            lst_candidate_lemma_keys: List[str],
                            lst_candidate_similarities: List[float],
                            top_k_candidates:int = 2,
                            ) -> Tuple[List[str], List[float]]:

        assert vec_query_embedding.ndim == 2, f"unexpected dimension size: {vec_query_embedding.ndim}"
        assert len(lst_candidate_similarities) == len(lst_candidate_lemma_keys), f"length must be identical:\n{lst_candidate_lemma_keys}\n{lst_candidate_similarities}"

        # do nothing if there is single candidate.
        if len(lst_candidate_lemma_keys) == 1:
            return lst_candidate_lemma_keys, lst_candidate_similarities

        # top-k most similar lemma keys
        lst_tup_lemma_key_and_similarity_top_k = sorted(zip(lst_candidate_lemma_keys, lst_candidate_similarities), key=lambda pair: pair[-1], reverse=True)[:top_k_candidates]

        # candidate synsets = {synset id of lemma sense key: similarity}
        dict_try_again_synsets = {}
        dict_candidate_synset_similarities = {}
        map_lemma_key_to_synset_id = {}
        for lemma_key, similarity in lst_tup_lemma_key_and_similarity_top_k:
            synset_id = utils_wordnet_gloss.lemma_key_to_synset_id(lemma_key)
            dict_candidate_synset_similarities[synset_id] = similarity
            map_lemma_key_to_synset_id[lemma_key] = synset_id

        if len(dict_candidate_synset_similarities) == 1:
            if self._do_not_fix_synset_degeneration_bug:
                print(f"there is only single candidate synset. we will return least similar sense key following original SREF implementation.")
                # it always return last (=originally least similar) sense key due to the implementation bug.
                least_similar_lemma = lst_tup_lemma_key_and_similarity_top_k[-1][0]
                least_similar_lemma_index = lst_candidate_lemma_keys.index(least_similar_lemma)
                lst_candidate_similarities[least_similar_lemma_index] += float("inf")
                return lst_candidate_lemma_keys, lst_candidate_similarities

        # collect semantically related synsets
        for candidate_synset_id in dict_candidate_synset_similarities.keys():
            dict_try_again_synsets[candidate_synset_id] = set(gloss_extend(candidate_synset_id, self._semantic_relation))

        # remove common synsets from semantically related synsets
        if self._exclude_common_semantically_related_synsets and (len(dict_candidate_synset_similarities) > 1):
            lst_set_synsets = list(dict_try_again_synsets.values())
            set_common_extended_synsets = set().union(*lst_set_synsets).intersection(*lst_set_synsets)
            for candidate_synset_id in dict_candidate_synset_similarities.keys():
                dict_try_again_synsets[candidate_synset_id] -= set_common_extended_synsets

        is_different_lexname = len(set(map(utils_wordnet_gloss.synset_id_to_lexname, dict_candidate_synset_similarities.keys()))) > 1
        for candidate_synset_id in dict_candidate_synset_similarities.keys():
            # if supersense is different, then extend semantically related synsets with lexnames
            if is_different_lexname:
                lexname = utils_wordnet_gloss.synset_id_to_lexname(candidate_synset_id)
                dict_try_again_synsets[candidate_synset_id] |= get_lexname_synsets(lexname)

            # compute try-again similarity using semantically related synsets
            lst_try_again_similarities = []
            for try_again_synset in dict_try_again_synsets[candidate_synset_id]:
                # exclude oneselves for NOUN and VERB
                if self._exclude_oneselves_for_noun_and_verb:
                    if try_again_synset.name() in dict_candidate_synset_similarities.keys() and (pos in ['n','v']):
                        continue

                # calculate similarity between query and lemmas which belong to try-again synset.
                lst_lemma_keys = utils_wordnet_gloss.synset_to_lemma_keys(try_again_synset)
                if self._lookup_first_lemma_sense_only:
                    lst_lemma_keys = lst_lemma_keys[:1]
                mat_gloss_embeddings = self._lemma_key_embeddings_dataset.get_lemma_key_embeddings(lst_lemma_keys)

                if self._similarity_metric == "cosine":
                    v_sim = cosine_similarity(vec_query_embedding.reshape(1,-1), mat_gloss_embeddings).flatten()
                elif self._similarity_metric == "dot":
                    v_sim = np.sum(vec_query_embedding.reshape(1,-1)*mat_gloss_embeddings, axis=-1)

                if self._average_similarity_in_synset:
                    lst_try_again_similarities.append(np.mean(v_sim))
                else:
                    lst_try_again_similarities.extend(v_sim.tolist())

            try_again_similarity = max(lst_try_again_similarities) if len(lst_try_again_similarities) > 0 else 0.0
            dict_candidate_synset_similarities[candidate_synset_id] = try_again_similarity

        # summand original similarity with try-again similarity.
        for lemma_key, original_similarity in lst_tup_lemma_key_and_similarity_top_k:
            synset_id = map_lemma_key_to_synset_id[lemma_key]
            try_agian_similarity = dict_candidate_synset_similarities[synset_id]
            idx = lst_candidate_lemma_keys.index(lemma_key)
            lst_candidate_similarities[idx] = original_similarity + try_agian_similarity

        return lst_candidate_lemma_keys, lst_candidate_similarities



class TryAgainMechanismWithCoarseSenseInventory(object):

    def __init__(self,
                 lemma_key_embeddings_dataset: SREFLemmaEmbeddingsDataset,
                 path_coarse_sense_inventory: str,
                 similarity_metric: str = "cosine",
                 device: Optional[str] = "cpu",
                 verbose: bool = False):
        """
        Try-again Mechanism algorithm combined with Coarse Sense Inventory[Lacerra+, 2020] that is proposed in [Wang and Wang, ACL2021].
        Reference: https://github.com/lwmlyy/SACE

        You just need to specify the path to the Coarse Sense Inventory.

        Args:
            path_coarse_sense_inventory: path to the Coarse Sense Inventory: `wn_synset2csi.txt`
        """

        self._lemma_key_embeddings_dataset = lemma_key_embeddings_dataset
        self._path_coarse_sense_inventory = path_coarse_sense_inventory

        if similarity_metric not in ("cosine","dot"):
            raise ValueError(f"invalid `similarity_module` name: {similarity_metric}")
        self._similarity_metric = similarity_metric

        self.verbose = verbose
        self._device = device

        dict_synset_id_to_csi_labels, dict_csi_label_to_synsets = self.load_coarse_sense_inventory_dataset(path=path_coarse_sense_inventory)
        self._dict_synset_id_to_csi_labels = dict_synset_id_to_csi_labels
        self._dict_csi_label_to_synsets = dict_csi_label_to_synsets

    @classmethod
    def load_coarse_sense_inventory_dataset(cls, path: str):

        dict_synset_id_to_csi_labels = {}
        with io.open(path, mode="r") as ifs:
            for record in ifs:
                lst_items = record.strip().split("\t")
                assert len(lst_items) >= 2, f"invaid record: {record}"
                synset_index, lst_csi_labels = lst_items[0], lst_items[1:]

                synset_id = utils_wordnet_gloss.synset_offset_to_synset_id(synset_index)
                assert synset_id not in dict_synset_id_to_csi_labels, f"duplicate synset id found: {synset_index} / {synset_id}"
                dict_synset_id_to_csi_labels[synset_id] = lst_csi_labels

        dict_csi_label_to_synset_ids = defaultdict(set)
        for synset_id, lst_csi_labels in dict_synset_id_to_csi_labels.items():
            for csi_label in lst_csi_labels:
                dict_csi_label_to_synset_ids[csi_label].add(wn.synset(synset_id))

        return dict_synset_id_to_csi_labels, dict_csi_label_to_synset_ids

    def synset_id_to_csi_labels(self, synset_id: str) -> List[str]:
        return self._dict_synset_id_to_csi_labels.get(synset_id, None)

    def csi_label_to_synsets(self, csi_label: str) -> Set["wn.Synset"]:
        return self._dict_csi_label_to_synsets[csi_label]

    def try_again_mechanism(self,
                            vec_query_embedding: torch.Tensor,
                            pos: str,
                            lst_candidate_lemma_keys: List[str],
                            lst_candidate_similarities: List[float],
                            top_k_candidates:int = 2,
                            ) -> Tuple[List[str], List[float]]:

        assert vec_query_embedding.ndim == 2, f"unexpected dimension size: {vec_query_embedding.ndim}"
        assert len(lst_candidate_similarities) == len(lst_candidate_lemma_keys), f"length must be identical:\n{lst_candidate_lemma_keys}\n{lst_candidate_similarities}"

        # do nothing if there is single candidate.
        if len(lst_candidate_lemma_keys) == 1:
            return lst_candidate_lemma_keys, lst_candidate_similarities

        # top-k most similar lemma keys
        lst_tup_lemma_key_and_similarity_top_k = sorted(zip(lst_candidate_lemma_keys, lst_candidate_similarities), key=lambda pair: pair[-1], reverse=True)[:top_k_candidates]

        # candidate synsets = dict{synset id of lemma sense key: similarity}
        dict_try_again_synsets = {}
        dict_candidate_synset_similarities = {}
        map_lemma_key_to_synset_id = {}
        for lemma_key, similarity in lst_tup_lemma_key_and_similarity_top_k:
            synset_id = utils_wordnet_gloss.lemma_key_to_synset_id(lemma_key)
            dict_candidate_synset_similarities[synset_id] = similarity
            map_lemma_key_to_synset_id[lemma_key] = synset_id

        if len(dict_candidate_synset_similarities) == 1:
            pass

        for candidate_synset_id in dict_candidate_synset_similarities.keys():
            dict_try_again_synsets[candidate_synset_id] = set()

        # extend semantically related synsets with same CSI classes
        for candidate_synset_id in dict_candidate_synset_similarities.keys():
            lst_csi_labels = self.synset_id_to_csi_labels(candidate_synset_id)
            if lst_csi_labels is not None:
                for csi_label in lst_csi_labels:
                    dict_try_again_synsets[candidate_synset_id] |= self.csi_label_to_synsets(csi_label)

            # compute try-again similarity using semantically related synsets
            lst_try_again_similarities = []
            for try_again_synset in dict_try_again_synsets[candidate_synset_id]:
                # calculate similarity between query and lemmas which belong to try-again synset.
                lst_lemma_keys = utils_wordnet_gloss.synset_to_lemma_keys(try_again_synset)
                mat_gloss_embeddings = self._lemma_key_embeddings_dataset.get_lemma_key_embeddings(lst_lemma_keys)

                if self._similarity_metric == "cosine":
                    v_sim = cosine_similarity(vec_query_embedding.reshape(1,-1), mat_gloss_embeddings).flatten()
                elif self._similarity_metric == "dot":
                    v_sim = np.sum(vec_query_embedding.reshape(1,-1)*mat_gloss_embeddings, axis=-1)

                lst_try_again_similarities.extend(v_sim.tolist())

            try_again_similarity = max(lst_try_again_similarities) if len(lst_try_again_similarities) > 0 else 0.0
            dict_candidate_synset_similarities[candidate_synset_id] = try_again_similarity

        # summand original similarity with try-again similarity.
        for lemma_key, original_similarity in lst_tup_lemma_key_and_similarity_top_k:
            synset_id = map_lemma_key_to_synset_id[lemma_key]
            try_agian_similarity = dict_candidate_synset_similarities[synset_id]
            idx = lst_candidate_lemma_keys.index(lemma_key)
            lst_candidate_similarities[idx] = original_similarity + try_agian_similarity

        return lst_candidate_lemma_keys, lst_candidate_similarities
