import json
import os
import random
from collections import defaultdict
from collections.abc import Callable
from csv import DictReader
from fractions import Fraction
from typing import Any

import torch
from pytorchvideo.data import ClipSampler, LabeledVideoDataset
from pytorchvideo.data.clip_sampling import ClipInfo
from pytorchvideo.data.video import VideoPathHandler
from torch.utils.data import Dataset

from video_blip.data.utils import C_REGEX


class NarratedActionClipSampler(ClipSampler):
    def __init__(self, random: bool) -> None:
        """The vast majority of narrated actions are 8 seconds long, and none
        are longer.

        So let's just sample 8-second clips.

        :param random: whether to return random clips or not
        """
        super().__init__(8)
        self.random = random
        self.sample_clip_indices: list[int] | None = None

    def __call__(
        self,
        last_clip_time: float | Fraction,
        video_duration: float | Fraction,
        annotation: dict[str, Any],
    ) -> ClipInfo:
        """Draw a random clip for a narrated action.

        :param last_clip_time: unused
        :param video_duration: unused
        :param annotation: narrated action data.
            See https://ego4d-data.org/docs/data/annotations-schemas/ for more details.
        """
        if self.sample_clip_indices is None:
            # first time sampling from this video, so create a clip index list
            self.sample_clip_indices = list(range(len(annotation["narrated_actions"])))
            if self.random:
                # shuffle them if random
                random.shuffle(self.sample_clip_indices)

        clip_index = self.sample_clip_indices[self._current_clip_index]
        narrated_action = annotation["narrated_actions"][clip_index]
        self._current_clip_index += 1

        is_last_clip = False
        if self._current_clip_index == len(self.sample_clip_indices):
            is_last_clip = True

        # sample a clip 8 seconds around narration_time_sec
        # if narration_time_sec is less than 4 seconds, we start from 0
        clip_start_sec = max(
            Fraction(narrated_action["narration_timestamp_sec"])
            - self._clip_duration / 2,
            0,
        )

        # add 8 seconds to clip_start_sec
        # if clip_end_sec goes over the video duration, adjust clip_start_sec
        clip_end_sec = clip_start_sec + self._clip_duration
        video_duration_sec = Fraction(annotation["video_metadata"]["duration_sec"])
        if clip_end_sec > video_duration_sec:
            clip_end_sec = video_duration_sec
            clip_start_sec = clip_end_sec - self._clip_duration

        if is_last_clip:
            self.reset()

        return ClipInfo(
            clip_start_sec,
            clip_end_sec,
            clip_index,
            0,
            is_last_clip,
        )

    def reset(self) -> None:
        self._current_clip_index = 0
        self.sample_clip_indices = None


def filter_action(action: dict[str, Any]) -> bool:
    """Return True if the given action should be used, False otherwise."""
    return (
        not action["is_rejected"]
        and action["is_valid_action"]
        and C_REGEX.match(action["narration_text"]) is not None
    )


def get_structured_noun(action: dict) -> str | None:
    if action["frames"] is None:
        return None
    for frame in action["frames"]:
        if frame["frame_type"] != "pnr_frame":
            # some actions don't have contact frames so use pnr_frame
            continue
        for box in frame["boxes"]:
            if (
                box["object_type"] == "object_of_change"
                and box["structured_noun"] is not None
            ):
                return box["structured_noun"]
    return None


class Ego4dFHOMainDataset(LabeledVideoDataset):
    def __init__(
        self,
        annotation_path: str,
        split_path: str,
        video_dir_path: str,
        transform: Callable[[dict], Any] | None = None,
        random_clip: bool = False,
    ) -> None:
        """
        :param annotation_path: path to the main annotation file, e.g., `fho_main.json`.
        :param split_path: path to video split file generated by
            `scripts/split_train_val_test.py`.
        :param video_path: path to video dir
        :param transform: optional transform function
        :param random_clip: whether to sample clips randomly
        """
        with open(annotation_path) as f:
            annotations = json.load(f)

        # create a dict video_uid => video
        video_dict = {video["video_uid"]: video for video in annotations["videos"]}

        with open(split_path) as f:
            split_data = json.load(f)

        self.split = split_data["split"]
        self.num_narrated_actions = sum(split_data["videos"].values())

        def _transform(item: dict) -> Any:
            """The first transform function that formats `narrated_actions` and
            `video`."""
            # format narrated_actions
            narrated_actions = item.pop("narrated_actions")
            item.update(narrated_actions[item["clip_index"]])

            # turn video tensor to torch.uint8
            item["video"] = item["video"].to(torch.uint8)
            if transform is not None:
                item = transform(item)
            return item

        super().__init__(
            [
                (
                    os.path.join(video_dir_path, video_uid + ".mp4"),
                    {
                        "narrated_actions": [
                            {
                                "narration_timestamp_sec": action[
                                    "narration_timestamp_sec"
                                ],
                                "narration_text": action["narration_text"],
                                "structured_verb": action["structured_verb"],
                                "structured_noun": get_structured_noun(action),
                            }
                            for interval in video_dict[video_uid]["annotated_intervals"]
                            for action in interval["narrated_actions"]
                            if filter_action(action)
                        ],
                        "video_uid": video_uid,
                        "video_metadata": video_dict[video_uid]["video_metadata"],
                    },
                )
                for video_uid in split_data["videos"]
            ],
            NarratedActionClipSampler(random_clip),
            transform=_transform,
            decode_audio=False,
        )

    def __len__(self) -> int:
        return self.num_narrated_actions


class Ego4dFHOMainFrameDataset(Dataset[dict[str, Any]]):
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


class Ego4dFHOMainFrameInterleavedDataset(Dataset[dict[str, Any]]):
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
        self._dataset = Ego4dFHOMainFrameDataset(narrated_actions_dir)
        self.in_context_example_narrated_actions_dir = (
            in_context_example_narrated_actions_dir
        )
        if in_context_example_narrated_actions_dir is None:
            self._in_context_dataset = self._dataset
        else:
            self._in_context_dataset = Ego4dFHOMainFrameDataset(
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
