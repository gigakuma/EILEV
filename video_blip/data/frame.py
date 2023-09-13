import os
import random
from collections import defaultdict
from collections.abc import Callable
from csv import DictReader
from typing import Any

import torch
from pytorchvideo.data.video import VideoPathHandler
from torch.utils.data import Dataset


class FrameDataset(Dataset[dict[str, Any]]):
    def __init__(
        self,
        narrated_actions_dir: str,
        transform: Callable[[dict[str, Any]], Any] | None = None,
        data_filter: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        """
        :param narrated_actions_dir: path to dir that contains narrated_actions.csv
            and extracted frames
        """
        self.narrated_actions_dir = narrated_actions_dir
        self.data: list[dict] = []
        with open(
            os.path.join(self.narrated_actions_dir, "narrated_actions.csv"), newline=""
        ) as csvfile:
            csvreader = DictReader(csvfile)
            for row in csvreader:
                if data_filter is not None and not data_filter(row):
                    continue
                self.data.append(row)

        self._video_path_handler = VideoPathHandler()
        self._transform = transform

    def __getitem__(self, index: int) -> dict[str, Any]:
        datapoint = self.data[index]
        video = self._video_path_handler.video_from_path(
            os.path.join(self.narrated_actions_dir, datapoint["frame_path"])
        )
        # just get the whole video since the clip is already extracted
        clip = video.get_clip(0, video.duration)

        item = {"video": clip["video"].to(torch.uint8), **datapoint}

        if self._transform is not None:
            item = self._transform(item)
        return item

    def __len__(self) -> int:
        return len(self.data)


class FrameInterleavedDataset(Dataset[dict[str, Any]]):
    def __init__(
        self,
        narrated_actions_dir: str,
        in_context_example_narrated_actions_dir: str | None = None,
        num_in_context_examples_per_sample: int = 4,
        verb_noun_ratio: float = 0.5,
        transform: Callable[[dict], Any] | None = None,
    ) -> None:
        self.num_in_context_examples_per_sample = num_in_context_examples_per_sample
        self.verb_noun_ratio = verb_noun_ratio
        self._dataset = FrameDataset(narrated_actions_dir)
        self.in_context_example_narrated_actions_dir = (
            in_context_example_narrated_actions_dir
        )
        if in_context_example_narrated_actions_dir is None:
            self._in_context_dataset = self._dataset
        else:
            self._in_context_dataset = FrameDataset(
                in_context_example_narrated_actions_dir
            )

        # put datapoints into buckets based on their structured verbs and nouns
        self.structured_verb_buckets: dict[str, set[int]] = defaultdict(set)
        self.structured_noun_buckets: dict[str, set[int]] = defaultdict(set)
        for i, datapoint in enumerate(self._in_context_dataset.data):
            if datapoint["structured_verb"] not in {"", "[other]"}:
                self.structured_verb_buckets[datapoint["structured_verb"]].add(i)
            if datapoint["structured_noun"] != "":
                self.structured_noun_buckets[datapoint["structured_noun"]].add(i)

        self._transform = transform

    def __getitem__(self, index: int) -> dict[str, Any]:
        datapoint = self._dataset[index]

        verb_bucket: set[int] = set()
        for i in self.structured_verb_buckets.get(datapoint["structured_verb"], set()):
            if self.in_context_example_narrated_actions_dir is None and i == index:
                # filter out the current example if the in-context example
                # dataset is the same as the main dataset
                continue
            verb_bucket.add(i)
        noun_bucket: set[int] = set()
        for i in self.structured_noun_buckets.get(datapoint["structured_noun"], set()):
            if self.in_context_example_narrated_actions_dir is None and i == index:
                # filter out the current example if the in-context example
                # dataset is the same as the main dataset
                continue
            noun_bucket.add(i)

        def _sample(bucket: set[int], k: int) -> set[int]:
            if len(bucket) >= k:
                samples = set(random.sample(bucket, k))
            else:
                samples = set(bucket)
            bucket -= samples
            return samples

        examples: set[int] = set()
        num_additional_examples = self.num_in_context_examples_per_sample - len(
            examples
        )
        while num_additional_examples > 0 and (
            len(verb_bucket) > 0 or len(noun_bucket) > 0
        ):
            if len(verb_bucket) > 0 and len(noun_bucket) > 0:
                num_verb_examples = int(num_additional_examples * self.verb_noun_ratio)
                num_noun_examples = num_additional_examples - num_verb_examples
            elif len(verb_bucket) == 0:
                num_verb_examples = 0
                num_noun_examples = num_additional_examples
            else:
                num_noun_examples = 0
                num_verb_examples = num_additional_examples

            examples |= _sample(verb_bucket, num_verb_examples)
            examples |= _sample(noun_bucket, num_noun_examples)
            num_additional_examples = self.num_in_context_examples_per_sample - len(
                examples
            )

        if num_additional_examples > 0:
            # there wasn't enough samples in verb and noun buckets, so sample from the
            # rest of the dataset
            rest: set[int] = set()
            for i in range(len(self._in_context_dataset)):
                if (
                    self.in_context_example_narrated_actions_dir is None and i == index
                ) or (i in examples):
                    # filter out the current example if the in-context example
                    # dataset is the same as the main dataset or
                    # it's already been drawn.
                    continue
                rest.add(i)
            examples |= _sample(rest, num_additional_examples)

        # shuffle the in-context examples and append the main datapoint in the end
        item = {
            "items": [
                self._in_context_dataset[i]
                for i in random.sample(examples, len(examples))
            ]
            + [datapoint]
        }
        if self._transform is not None:
            item = self._transform(item)
        return item

    def __len__(self) -> int:
        return len(self._dataset)
