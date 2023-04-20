"""
ai4eps.py: this file contains dataset following standard AI4EPS format.
Reference: https://ai4eps.github.io/homepage/ml4earth/seismic_event_format1/
"""
from pathlib import Path
from typing import List, Optional, Tuple

import h5py
import numpy as np
import pandas as pd
import torch
from torch.utils.data import Dataset

from src.data.components.utils import (generate_label, normalize_waveform,
                                       stack_rand)


class Ai4epsDataset(Dataset):
    def __init__(self, data_dir: Path, index_to_waveform_id: List[Tuple[str, str]] = [], transform: Optional[callable] = None, label_shape: str = "gaussian", label_width_in_npts: int = 120, window_length_in_npts: int = 4800, phases: List[str] = ["P", "S", "PS"], first_arrival_index_in_final_window_if_no_shift: int = 400, random_stack_two_waveforms_ratio=0.0):
        """
        Args:
            data_dir (Path): the directory of the dataset
            index_to_waveform_id (List[Tuple[str, str]], optional): list of tuples, each tuple is (event_id, station_id). Defaults to []. Only the waveforms in the list will be used.
            transform (Optional[callable], optional): Optional transform to be applied on a sample. Defaults to None.
            label_shape (str, optional): the shape of the label, can be "gaussian" or "triangle". Defaults to "gaussian".
            label_width_in_npts (int, optional): the width of the label in number of points. Defaults to 120.
            window_length_in_npts (int, optional): the length of the window in number of points. Defaults to 4800.
            phases (List[str], optional): list of phases. Defaults to ["P", "S", "PS"].
            first_arrival_index_in_final_window_if_no_shift (int, optional): the index of the first arrival in the final window if no shift. Defaults to 400.
            random_stack_two_waveforms_ratio (float, optional): the ratio of stacking two waveforms. Defaults to 0.0.
        """
        self.transform = transform
        self.label_shape = label_shape
        self.label_width_in_npts = label_width_in_npts
        self.window_length_in_npts = window_length_in_npts
        self.phases = phases
        self.first_arrival_index_in_final_window_if_no_shift = first_arrival_index_in_final_window_if_no_shift
        self.random_stack_two_waveforms_ratio = random_stack_two_waveforms_ratio

        # waveform.h5 is a hdf5 file containing waveform data
        # eg. f["11_52111"]["A01"][...] is a 3XNT numpy array
        # f["11_52111"]["A01"].attrs is the attributes of the waveform
        self.h5py_path = data_dir / "waveform.h5"
        self._handler = None
        # index_to_waveform_id is a list of tuples, each tuple is (event_id, station_id)
        # eg. [("11_52111", "A01"), ("11_52111", "A02"), ...]
        self.index_to_waveform_id = index_to_waveform_id

    @property
    def handler(self) -> h5py.File:
        """
        Returns:
            h5py.File: the handler of the hdf5 file
        """
        if self._handler is None:
            # lazy load the hdf5 file to avoid pickle error
            self._handler = h5py.File(self.h5py_path, "r")
        return self._handler

    def __len__(self) -> int:
        """ 
        Returns:
            int: the total number of waveforms in the dataset
        """
        return len(self.index_to_waveform_id)

    def get_item_without_stack(self, idx) -> dict:
        """
        Args:
            idx (int): the index of the waveform
        Returns:    
            dict: a sample containing waveform data, phase index, phase type, event id, network, station id
        """
        event_id, station_id = self.index_to_waveform_id[idx]
        waveform = torch.tensor(
            self.handler[event_id][station_id][...], dtype=torch.float32)
        attrs = self.handler[event_id][station_id].attrs

        sample = {
            "event_id": event_id,
            "network": attrs["network"],
            "station_id": station_id,
            "data": waveform,
            "phase_index": attrs["phase_index"].tolist(),
            "phase_type": attrs["phase_type"].tolist(),
        }
        min_index = min(sample["phase_index"])
        start_index, end_index = min_index - \
            self.first_arrival_index_in_final_window_if_no_shift, min_index - \
            self.first_arrival_index_in_final_window_if_no_shift+self.window_length_in_npts

        # used by transforms to indicate the start and end index of the window
        sample["start_index"] = start_index
        sample["end_index"] = end_index

        if self.transform:
            sample = self.transform(sample)

        # cut sample['data'] to the window length
        sample['data'] = sample['data'][:, start_index:end_index]
        # shift the phase index to the window length
        sample['phase_index'] = [i-start_index for i in sample['phase_index']]
        # generate label, arrivals should be in order as self.phases, if not exist, use -1
        expanded_phase_index = []
        for phase in self.phases:
            if phase in sample['phase_type']:
                expanded_phase_index.append(
                    sample['phase_index'][sample['phase_type'].index(phase)])
            else:
                expanded_phase_index.append(-999999999)
        sample['phase_index'] = expanded_phase_index
        sample['phase_type'] = self.phases
        # convert phase_idnex to tensor, otherwise in dataloader, it will be converted to list with wrong shape
        # eg: using list: 3X8 if batch_size=8, using tensor: 8X3
        sample['phase_index'] = torch.tensor(sample['phase_index'])

        sample['label'] = generate_label(
            self.label_shape, self.label_width_in_npts, self.window_length_in_npts, sample['phase_index'])
        # normalize the data before possible stacking
        sample = normalize_waveform(sample)

        return sample

    def __getitem__(self, idx) -> dict:
        """
        Args:
            idx (int): the index of the waveform
        Returns:
            dict: a sample containing waveform data, phase index, phase type, event id, network, station id
        """
        # the difference between get_item_without_stack and __getitem__ is that get_item_without_stack does not stack two waveforms
        # the stack ratio can be obtained by self.random_stack_two_waveforms_ratio
        current_sample = self.get_item_without_stack(idx)
        if torch.rand(1) < self.random_stack_two_waveforms_ratio:
            random_idx = torch.randint(0, len(self), (1,)).item()
            random_sample = self.get_item_without_stack(random_idx)
            current_sample = stack_rand(
                current_sample, random_sample, self.label_width_in_npts)

        # normalize the data at the end
        current_sample = normalize_waveform(current_sample)
        # remove unused keys, including start_index, end_index
        current_sample.pop('start_index', None)
        current_sample.pop('end_index', None)

        return current_sample


def split_train_test_val_for_ai4eps(data_dir: Path, ratio: List[float] = [0.9, 0.05, 0.05], seed: int = 3407) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]:
    """
    Split the dataset into train, test and val set
    Args:
        data_dir (Path): the directory of the dataset
        ratio (List[float], optional): the ratio of train, test and val. Defaults to [0.9, 0.05, 0.05].
        seed (int, optional): the seed for random shuffle. Defaults to 3407.
    Returns:
        Tuple[List[Tuple[str, str]], List[Tuple[str, str]], List[Tuple[str, str]]]: train, test and val set
    """
    # csv columns: event_id,station_id,phase_index,phase_time,phase_score,phase_type,phase_polarity
    phase_picks = pd.read_csv(data_dir / "phase_picks.csv")
    # get all distinct (event_id, station_id) pairs
    unique_event_station_pairs = phase_picks[[
        "event_id", "station_id"]].drop_duplicates().values
    # split the pairs into train, test and val, result should be lists of tuples
    rng = np.random.default_rng(seed)
    rng.shuffle(unique_event_station_pairs)
    train, test, val = np.split(unique_event_station_pairs, [
                                int(ratio[0]*len(unique_event_station_pairs)), int((ratio[0]+ratio[1])*len(unique_event_station_pairs))])
    return train.tolist(), test.tolist(), val.tolist()
