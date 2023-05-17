import argparse
import string
from functools import partial
from pathlib import Path

import gradio as gr
import torch
from pytorchvideo.data.video import VideoPathHandler
from transformers import Blip2Processor

from video_blip.model import VideoBlipForConditionalGeneration


@torch.no_grad()
def respond(
    model: VideoBlipForConditionalGeneration,
    processor: Blip2Processor,
    video_path_handler: VideoPathHandler,
    video_path: str,
    message: str,
    chat_history: list[list[str]],
    num_beams: int,
    max_new_tokens: int,
    temperature: float,
) -> tuple[str, list[list[str]]]:
    # process only the first 10 seconds
    clip = video_path_handler.video_from_path(video_path).get_clip(0, 10)

    # sample a frame every 30 frames, i.e. 1 fps. We assume the video is 30 fps for now.
    frames = clip["video"][:, ::30, ...]

    # construct chat context
    context = " ".join(user_msg + " " + bot_msg for user_msg, bot_msg in chat_history)
    context = context + " " + message.strip()
    context = context.strip()

    # process the inputs
    inputs = processor(
        images=frames.permute(1, 0, 2, 3), text=context, return_tensors="pt"
    ).to(model.device)
    inputs["pixel_values"] = inputs["pixel_values"].permute(1, 0, 2, 3).unsqueeze(0)
    generated_ids = model.generate(
        **inputs,
        num_beams=num_beams,
        max_new_tokens=max_new_tokens,
        temperature=temperature
    )
    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=True)[
        0
    ].strip()

    # if the last character of the generated text is not a punctuation, add a period
    if generated_text[-1] not in string.punctuation:
        generated_text += "."

    chat_history.append([message, generated_text])

    # return an empty string to clear out the chat input text box
    return "", chat_history


def construct_demo(
    model: VideoBlipForConditionalGeneration,
    processor: Blip2Processor,
    video_path_handler: VideoPathHandler,
) -> gr.Blocks:
    with gr.Blocks() as demo:
        gr.Markdown("# VideoBLIP Demo")
        gr.Markdown("Upload a video and have a conversation about it with VideoBLIP!")
        gr.Markdown(
            """**Limitations**
- Due to computational limits, VideoBLIP only processes the first 10 seconds of the uploaded video.
- If you use a non-instruction-tuned LLM backbone, it may not be able to perform multi-turn dialogues.
- If you still want to chat with a non-instruction-tuned LLM backbone, try formatting your input as \"Question: {} Answer: \""""  # noqa: E501
        )
        with gr.Row():
            with gr.Column():
                with gr.Row():
                    video_input = gr.Video()
                with gr.Row():
                    num_beams = gr.Slider(
                        minimum=0, maximum=10, value=4, step=1, label="Number of beams"
                    )
                    max_new_tokens = gr.Slider(
                        minimum=20, maximum=256, value=128, label="Maximum new tokens"
                    )
                    temp = gr.Slider(
                        minimum=0.1, maximum=1.0, value=0.7, label="Temperature"
                    )
            with gr.Column():
                with gr.Row():
                    chatbot = gr.Chatbot()
                with gr.Row():
                    chat_input = gr.Textbox()
                    chat_input.submit(
                        partial(respond, model, processor, video_path_handler),
                        inputs=[
                            video_input,
                            chat_input,
                            chatbot,
                            num_beams,
                            max_new_tokens,
                            temp,
                        ],
                        outputs=[chat_input, chatbot],
                    )
                with gr.Row():
                    clear_button = gr.Button(value="Clear")
                    clear_button.click(lambda: ("", []), outputs=[chat_input, chatbot])
        with gr.Row():
            curr_path = Path(__file__).parent
            gr.Examples(
                examples=[
                    [
                        str(curr_path / "examples/bike-fixing-0.mp4"),
                        "What is the camera wearer doing?",
                    ],
                    [
                        str(curr_path / "examples/bike-fixing-1.mp4"),
                        "Question: What is the camera wearer doing? Answer:",
                    ],
                    [
                        str(curr_path / "examples/motorcycle-riding-0.mp4"),
                        "What is the camera wearer doing?",
                    ],
                    [
                        str(curr_path / "examples/motorcycle-riding-1.mp4"),
                        "Question: What is the camera wearer doing? Answer:",
                    ],
                ],
                inputs=[video_input, chat_input],
            )

    return demo


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model_name_or_path")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    processor = Blip2Processor.from_pretrained(args.model_name_or_path)
    model = VideoBlipForConditionalGeneration.from_pretrained(
        args.model_name_or_path
    ).to(args.device)
    demo = construct_demo(model, processor, VideoPathHandler())
    demo.launch()