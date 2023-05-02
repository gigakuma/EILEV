from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any

import torch
import transformers
from pytorchvideo.transforms import UniformTemporalSubsample
from torchvision.transforms import Compose
from transformers import Blip2Processor
from transformers.deepspeed import is_deepspeed_zero3_enabled

from video_blip2.dataset.ego4d import Ego4dFHOMainDataset
from video_blip2.dataset.utils import clean_narration_text
from video_blip2.model import VideoBlip2ForConditionalGeneration

PROMPT = "Question: What is the camera wearer doing? Answer:"
INSTR_PROMPT = "What is the camera wearer doing?"


def preprocess(
    processor: Blip2Processor,
    item: dict[str, Any],
    video_transform: Callable[[torch.Tensor], torch.Tensor] | None = None,
    decoder_only_lm: bool = True,
    instruct_tuned: bool = True,
) -> dict[str, torch.Tensor]:
    prompt = INSTR_PROMPT if instruct_tuned else PROMPT

    # tokenize text inputs
    cleaned_narration_text = clean_narration_text(item["narration_text"])
    if decoder_only_lm:
        # tokenize and append eos
        preprocessed = processor.tokenizer(
            prompt + " " + cleaned_narration_text, return_attention_mask=False
        )
        preprocessed.input_ids.append(processor.tokenizer.eos_token_id)
        preprocessed["input_ids"] = torch.tensor(preprocessed.input_ids)
    else:
        # eos is automatically appended by the tokenizer
        preprocessed = processor.tokenizer(
            prompt, return_attention_mask=False, return_tensors="pt"
        )
        preprocessed["labels"] = processor.tokenizer(
            cleaned_narration_text, return_attention_mask=False
        ).input_ids

    # transform video inputs
    pixel_values = item["video"]
    if video_transform is not None:
        pixel_values = video_transform(pixel_values)

    # run pixel_values through the image processor
    pixel_values = processor.image_processor(
        pixel_values.permute(1, 0, 2, 3), return_tensors="pt"
    )["pixel_values"].permute(1, 0, 2, 3)
    preprocessed["pixel_values"] = pixel_values

    return preprocessed


# NOTE: We can't use 3.10's new X|Y syntax b/c HfArgumentParser doesn't support it.
# https://github.com/huggingface/transformers/issues/20249
@dataclass
class ModelArguments:
    model_name_or_path: str
    instruct_tuned: bool
    num_subsample_frames: int


@dataclass
class DataArguments:
    annotation_path: str
    train_split_path: str
    val_split_path: str
    video_dir_path: str


@dataclass
class TrainingArguments(transformers.TrainingArguments):
    optim: str = field(default="adamw_torch")


class DataCollatorForVideoLanguageModeling(
    transformers.DataCollatorForLanguageModeling
):
    def __call__(self, features, return_tensors=None):
        pixel_values = torch.stack(
            [feature.pop("pixel_values") for feature in features]
        )
        collated = super().__call__(features, return_tensors=return_tensors)
        collated["pixel_values"] = pixel_values
        return collated


class DataCollatorForVideoSeq2Seq(transformers.DataCollatorForSeq2Seq):
    def __call__(self, features, return_tensors=None):
        pixel_values = torch.stack(
            [feature.pop("pixel_values") for feature in features]
        )
        collated = super().__call__(features, return_tensors=return_tensors)
        collated["pixel_values"] = pixel_values
        return collated


def train() -> None:
    parser = transformers.HfArgumentParser(
        (ModelArguments, DataArguments, TrainingArguments)
    )
    model_args: ModelArguments
    data_args: DataArguments
    training_args: TrainingArguments
    model_args, data_args, training_args = parser.parse_args_into_dataclasses()

    # Don't remove "unused columns" such as clip-related columns
    training_args.remove_unused_columns = False

    processor = transformers.Blip2Processor.from_pretrained(
        model_args.model_name_or_path
    )
    model = VideoBlip2ForConditionalGeneration.from_pretrained(
        model_args.model_name_or_path,
        low_cpu_mem_usage=False if is_deepspeed_zero3_enabled() else True,
    )
    # freeze everything except for qformer
    for param in model.vision_model.parameters():
        param.requires_grad = False
    for param in model.language_model.parameters():
        param.requires_grad = False
    # we need to enable input require grads since the vision model (the first layer) is
    # frozen.
    model.enable_input_require_grads()

    train_data = Ego4dFHOMainDataset(
        data_args.annotation_path,
        data_args.train_split_path,
        data_args.video_dir_path,
        transform=partial(
            preprocess,
            processor,
            video_transform=Compose(
                [UniformTemporalSubsample(model_args.num_subsample_frames)]
            ),
            decoder_only_lm=model.config.use_decoder_only_language_model,
            instruct_tuned=model_args.instruct_tuned,
        ),
    )
    val_data = Ego4dFHOMainDataset(
        data_args.annotation_path,
        data_args.val_split_path,
        data_args.video_dir_path,
        transform=partial(
            preprocess,
            processor,
            video_transform=Compose(
                [UniformTemporalSubsample(model_args.num_subsample_frames)]
            ),
            decoder_only_lm=model.config.use_decoder_only_language_model,
            instruct_tuned=model_args.instruct_tuned,
        ),
    )

    # Load the best model at the end so we can save it
    training_args.load_best_model_at_end = True

    trainer = transformers.Trainer(
        model=model,
        args=training_args,
        train_dataset=train_data,
        eval_dataset=val_data,
        data_collator=DataCollatorForVideoLanguageModeling(
            processor.tokenizer,
            mlm=False,
            pad_to_multiple_of=8 if training_args.fp16 or training_args.bf16 else None,
        )
        if model.config.use_decoder_only_language_model
        else DataCollatorForVideoSeq2Seq(
            processor.tokenizer,
            pad_to_multiple_of=8 if training_args.fp16 or training_args.bf16 else None,
        ),
    )
    trainer.train(resume_from_checkpoint=training_args.resume_from_checkpoint)
    model.save_pretrained(training_args.output_dir)
    processor.save_pretrained(training_args.output_dir)


if __name__ == "__main__":
    train()
