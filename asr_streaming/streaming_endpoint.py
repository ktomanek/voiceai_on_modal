# Streaming-based ASR based on websockets. Two ASR models (Whisper and Parakeet) are supported.
#
# Below code is based on this websocket and streaming example: 
# https://modal.com/docs/examples/streaming_parakeet#
#
# Note:
# Segmentation here is done using a silence detector -- only when a silence of a certain
# minimum length is detected, transcription is started for the previous segment.
# It is good for demonstration, but for practical streaming-based ASR one would rather do 
# a combination of silence detection (or VAD) and maximum audio-buffer size. Common issues 
# this current approach has are for example: momemts of silence may be transcribed (which will
# often lead to "hello" or "yeah" or "hmm" depending on the model). Sometimes small pieces in the
# audio are lost and hence untranscribed. Streaming does not feel real-time and adding pauses to trigger
# transcription is certainly artificial.
#
# For a better way to handle real-time streaming see for example
# here for a more responsive real-time streaming approach (on-device): https://github.com/ktomanek/captioning
#
# How to run:
# * modal deploy streaming_endpoint.py
# * Then open the created endpoint for either of the two models: 
#    https://xxx--streaming-endpoint-whisper-web.modal.run
#    https://xxx--streaming-endpoint-parakeet-web.modal.run

import asyncio
import os
import sys
from pathlib import Path

import modal

GPU = 'L4'
SCALEDOWN = 60 * 2 # seconds


################################
# A silence detector is used to determine segments for transcription.
# Transcription is triggered for the recording up to detected silence. Below parameters can be used 
# to adjust the silence detector. If you want to transcribe more frequently, reduce MIN_SILENCE_LEN.

# MIN_SILENCE_LEN (in milliseconds): This sets the minimum duration a quiet period must last to be 
# considered "silence." For example, if you set this to 1000, only quiet periods lasting 1 second or longer will be detected as silence. 
# Shorter quiet moments (like brief pauses between words) will be ignored.
MIN_SILENCE_LEN=300 # ms

# SILENCE_THRESHOLD (in dBFS - decibels relative to full scale): This sets the volume threshold below which audio is 
# considered "silent." It's typically a negative number (like -40 or -20). The more negative the value, the quieter the 
# audio needs to be to count as silence.
SILENCE_THRESHOLD=-40 # dB
################################

TARGET_SAMPLE_RATE = 16_000
MODAL_APP_NAME = "streaming_endpoint"
MODEL_MOUNT_DIR = Path("/models")
MODEL_DOWNLOAD_DIR = Path("downloads")

app = modal.App(MODAL_APP_NAME)
volume = modal.Volume.from_name(MODAL_APP_NAME, create_if_missing=True)

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.0-cudnn-devel-ubuntu22.04", add_python="3.12"
    )
    .env(
        {
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "HF_HOME": "/cache",  # cache directory for Hugging Face models
            "DEBIAN_FRONTEND": "noninteractive",
            "CXX": "g++",
            "CC": "g++",
        }
    )
    .apt_install("ffmpeg")
    .pip_install(
        "hf_transfer==0.1.9",
        "huggingface_hub[hf-xet]==0.31.2",
        "nemo_toolkit[asr]==2.3.0",
        "cuda-python==12.8.0",
        "fastapi==0.115.12",
        "numpy<2",
        "pydub==0.25.1",
        "torch",
        "ctranslate2",
        "faster_whisper",
        "librosa",
    )
    .entrypoint([])  # silence chatty logs by container on start
    .add_local_dir(  # changes fastest, so make this the last layer
        Path(__file__).parent / "web-frontend",
        remote_path="/frontend",
    )
)

def maybe_download_nemo_model(model_storage_dir, model_name):
    """Download NeMo Parakeet model if not available locally.
    (We want to avoid downloading the same model every time we start the endpoint).
    """
    import nemo.collections.asr as nemo_asr

    model_dir = model_storage_dir / model_name.replace("/", "_")
    model_file = model_dir / "model.bin"

    if not model_file.exists():
        print(f"Downloading model to {model_file} ...")            
        model_dir.mkdir(parents=True, exist_ok=True)
        model = nemo_asr.models.ASRModel.from_pretrained(model_name=model_name)
        model.save_to(str(model_file))
        print(f"Model downloaded successfully.")
    else:
        print(f"Model already available at {model_file}.")

    return str(model_file)


def maybe_download_whisper_model(model_storage_dir, model_id):
    """Download fasterwhisper model if not available locally.
    (We want to avoid downloading the same model every time we start the endpoint).
    """
    from faster_whisper.utils import download_model

    model_path = model_storage_dir / model_id

    if not model_path.exists():
        print(f"Downloading model to {model_path} ...")            
        model_path.mkdir(parents=True)
        download_model(model_id, output_dir=model_path)
        print(f"Model downloaded successfully.")
    else:
        print(f"Model already available on {model_path}.")

    return str(model_path)


async def handle_audio_chunk(
    transcriber,
    chunk: bytes,
    audio_segment,
    silence_thresh=SILENCE_THRESHOLD,
    min_silence_len=MIN_SILENCE_LEN,
):
    import pydub

    new_audio_segment = pydub.AudioSegment(
        data=chunk,
        channels=1,
        sample_width=2,
        frame_rate=TARGET_SAMPLE_RATE,
    )

    audio_segment += new_audio_segment

    silent_windows = pydub.silence.detect_silence(
        audio_segment,
        min_silence_len=min_silence_len,
        silence_thresh=silence_thresh,
    )

    if len(silent_windows) == 0:
        return audio_segment, None

    last_window = silent_windows[-1]

    if last_window[0] == 0 and last_window[1] == len(audio_segment):
        audio_segment = pydub.AudioSegment.empty()
        return audio_segment, None

    segment_to_transcribe = audio_segment[: last_window[1]]

    audio_segment = audio_segment[last_window[1] :]
    try:
        text = transcriber.transcribe(segment_to_transcribe.raw_data)
        return audio_segment, text
    except Exception as e:
        print("Transcription error:", e)
        raise e


@app.cls(
    image=image, 
    gpu=GPU, 
    scaledown_window=SCALEDOWN, 
    enable_memory_snapshot=True,
    volumes={MODEL_MOUNT_DIR: volume})
@modal.concurrent(max_inputs=14, target_inputs=10)
class Parakeet:
    @modal.enter()
    def load(self):
        import nemo.collections.asr as nemo_asr
        import logging

        # silence chatty logs from nemo
        logging.getLogger("nemo_logger").setLevel(logging.CRITICAL)

        model_dir = MODEL_MOUNT_DIR / MODEL_DOWNLOAD_DIR
        model_path = maybe_download_nemo_model(model_dir, "nvidia/parakeet-tdt-0.6b-v2")
        
        # Check if we have a saved model, otherwise load from pretrained
        if os.path.exists(model_path):
            self.model = nemo_asr.models.ASRModel.restore_from(model_path)
        else:
            self.model = nemo_asr.models.ASRModel.from_pretrained(
                model_name="nvidia/parakeet-tdt-0.6b-v2"
            )

    def transcribe(self, audio_bytes: bytes) -> str:
        import numpy as np

        audio_data = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)

        with NoStdStreams():  # hide output, see https://github.com/NVIDIA/NeMo/discussions/3281#discussioncomment-2251217
            output = self.model.transcribe([audio_data])

        return output[0].text


    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, Response, WebSocket
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles

        web_app = FastAPI()
        web_app.mount("/static", StaticFiles(directory="/frontend"))

        @web_app.get("/status")
        async def status():
            return Response(status_code=200)

        # serve frontend
        @web_app.get("/")
        async def index():
            return HTMLResponse(content=open("/frontend/index.html").read())

        @web_app.websocket("/ws")
        async def run_with_websocket(ws: WebSocket):
            from fastapi import WebSocketDisconnect
            import pydub

            await ws.accept()

            # initialize an empty audio segment
            audio_segment = pydub.AudioSegment.empty()

            try:
                while True:
                    chunk = await ws.receive_bytes()
                    audio_segment, text = await handle_audio_chunk(
                        self, chunk, audio_segment
                    )
                    if text:
                        await ws.send_text(text)
            except Exception as e:
                if not isinstance(e, WebSocketDisconnect):
                    print(f"Error handling websocket: {type(e)}: {e}")
                try:
                    await ws.close(code=1011, reason="Internal server error")
                except Exception as e:
                    print(f"Error closing websocket: {type(e)}: {e}")

        return web_app


@app.cls(
    image=image, 
    gpu=GPU, 
    scaledown_window=SCALEDOWN, 
    enable_memory_snapshot=True,
    volumes={MODEL_MOUNT_DIR: volume})
@modal.concurrent(max_inputs=14, target_inputs=10)
class Whisper:
    model_id = 'large-v3-turbo'
    
    @modal.enter()
    def load(self):
        from faster_whisper import WhisperModel
        
        model_dir = MODEL_MOUNT_DIR / MODEL_DOWNLOAD_DIR
        model_path = maybe_download_whisper_model(model_dir, self.model_id)
        self.model = WhisperModel(model_path, device="cuda", compute_type="float16")
        print(f"FasterWhisper model loaded from path: {model_path}")

    def transcribe(self, audio_bytes: bytes) -> str:
        import numpy as np

        # Convert raw bytes directly to numpy array (same as Parakeet approach)
        audio_data = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32)

        with NoStdStreams():
            segments, _ = self.model.transcribe(
                audio_data,
                beam_size=5,
                language=None,  # auto-detect
                task='transcribe',
                condition_on_previous_text=False,
                vad_filter=True,
            )
            
            transcription = ""
            for segment in segments:
                transcription += segment.text + " "
        
        return transcription.strip()

    @modal.asgi_app()
    def web(self):
        from fastapi import FastAPI, Response, WebSocket
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles

        web_app = FastAPI()
        web_app.mount("/static", StaticFiles(directory="/frontend"))

        @web_app.get("/status")
        async def status():
            return Response(status_code=200)

        @web_app.get("/")
        async def index():
            return HTMLResponse(content=open("/frontend/index.html").read())

        @web_app.websocket("/ws")
        async def run_with_websocket(ws: WebSocket):
            from fastapi import WebSocketDisconnect
            import pydub

            await ws.accept()

            audio_segment = pydub.AudioSegment.empty()

            try:
                while True:
                    chunk = await ws.receive_bytes()
                    audio_segment, text = await handle_audio_chunk(
                        self, chunk, audio_segment
                    )
                    if text:
                        await ws.send_text(text)
            except Exception as e:
                if not isinstance(e, WebSocketDisconnect):
                    print(f"Error handling websocket: {type(e)}: {e}")
                try:
                    await ws.close(code=1011, reason="Internal server error")
                except Exception as e:
                    print(f"Error closing websocket: {type(e)}: {e}")

        return web_app


class NoStdStreams(object):
    def __init__(self):
        self.devnull = open(os.devnull, "w")

    def __enter__(self):
        self._stdout, self._stderr = sys.stdout, sys.stderr
        self._stdout.flush(), self._stderr.flush()
        sys.stdout, sys.stderr = self.devnull, self.devnull

    def __exit__(self, exc_type, exc_value, traceback):
        sys.stdout, sys.stderr = self._stdout, self._stderr
        self.devnull.close()