import os
import sys
import io
import tempfile
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Form, UploadFile, File
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
import uuid
import numpy as np
import soundfile as sf
import torch
import asyncio
import json
import gc
from pydub import AudioSegment
from typing import List, Optional
import requests
import subprocess

APP_VERSION = "1.0.2" # Current application version
GITHUB_REPO = "parkerallen1/localTTSstudio" # Actual repo for OTA updates

# We attempt to import qwen_tts but catch the error if it fails during initial import
try:
    from qwen_tts import Qwen3TTSModel
    HAS_QWEN = True
except Exception as _e:
    import traceback
    traceback.print_exc()
    print(f"qwen_tts import failed: {_e}")
    HAS_QWEN = False

# Profile Storage Setup
if getattr(sys, 'frozen', False):
    DATA_DIR = os.path.expanduser("~/.qwen_tts_studio")
else:
    DATA_DIR = "data"

PROFILES_DIR = os.path.join(DATA_DIR, "profiles")
PROFILES_FILE = os.path.join(PROFILES_DIR, "profiles.json")
os.makedirs(PROFILES_DIR, exist_ok=True)

if not os.path.exists(PROFILES_FILE):
    with open(PROFILES_FILE, "w") as f:
        json.dump([], f)

# Built-in voice profile that is always available and cannot be deleted
BUILTIN_PROFILE_ID = "__builtin_default__"
BUILTIN_PROFILE = {
    "id": BUILTIN_PROFILE_ID,
    "name": "Jennifer",
    "ref_text": "Settle in. Take a deep breath. Turn off notifications on your phone if you can. Ask God to give you a new perspective and to help",
    "audio_path": os.path.join(os.path.dirname(__file__), "static", "builtin", "default_voice.wav"),
    "builtin": True
}

def load_profiles():
    with open(PROFILES_FILE, "r") as f:
        user_profiles = json.load(f)
    # Always prepend the built-in profile
    return [BUILTIN_PROFILE] + user_profiles

def save_profiles(profiles):
    # Filter out the built-in profile before saving
    user_profiles = [p for p in profiles if p.get("id") != BUILTIN_PROFILE_ID]
    with open(PROFILES_FILE, "w") as f:
        json.dump(user_profiles, f, indent=4)

# Global context for the model to keep it loaded
model = None
current_model_id = None
model_lock = None

# Global progress state
download_progress = {
    "status": "idle", # idle, downloading, extracting, ready, error
    "progress": 0.0,
    "description": ""
}

# We can hack huggingface_hub's tqdm to intercept progress
from huggingface_hub.utils import tqdm as hf_tqdm

class InterceptTqdm(hf_tqdm):
    def update(self, n=1):
        super().update(n)
        if hasattr(self, 'total') and self.total:
            pct = (self.n / self.total) * 100
            download_progress["progress"] = pct
            download_progress["status"] = "downloading"
            download_progress["description"] = self.desc or "Downloading..."

# Monkey patch huggingface hub tqdm
import huggingface_hub.utils as hf_utils
hf_utils.tqdm = InterceptTqdm

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    print("Shutting down... clearing models.")
    global model, current_model_id
    if model is not None:
        del model
        model = None
        current_model_id = None
        gc.collect()
        if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            torch.mps.empty_cache()
        elif torch.cuda.is_available():
            torch.cuda.empty_cache()

VALID_MODEL_SIZES = {"0.6B", "1.7B"}
VALID_MODEL_TYPES = {"Base", "CustomVoice", "VoiceDesign"}

def _load_model_sync(model_id: str, device: str, dtype: torch.dtype):
    """Synchronous function to load the model."""
    from qwen_tts import Qwen3TTSModel
    m = Qwen3TTSModel.from_pretrained(model_id, device_map=device, dtype=dtype)
    return m

async def get_tts_model(size: str = "1.7B", model_type: str = "CustomVoice"):
    global model, current_model_id, model_lock
    
    if model_lock is None:
        model_lock = asyncio.Lock()
    
    expected_model_id = f"Qwen/Qwen3-TTS-12Hz-{size}-{model_type}"
    
    if current_model_id == expected_model_id and model is not None:
        return model
        
    async with model_lock:
        if current_model_id == expected_model_id and model is not None:
            return model

        if not HAS_QWEN:
            print("qwen-tts package is not installed. TTS generation will not work.")
            download_progress["status"] = "error"
            download_progress["description"] = "qwen-tts not installed."
            raise RuntimeError("qwen-tts package is not installed.")

        # Free old model from memory if we are swapping
        if model is not None:
            print(f"Unloading existing model {current_model_id} to load {expected_model_id}...")
            del model
            model = None
            current_model_id = None
            gc.collect()
            if hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif torch.cuda.is_available():
                torch.cuda.empty_cache()

        device = "cpu"
        dtype = torch.float32
        if torch.cuda.is_available():
            device = "cuda:0"
            dtype = torch.bfloat16
        elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
            device = "mps"
            # bfloat16 has the same exponent range as float32 (avoids NaN/overflow),
            # but is half the size — so it runs at full MPS speed on all model sizes.
            # float16 has a narrower exponent and was causing overflow on 0.6B.
            dtype = torch.bfloat16

        print(f"Loading model {expected_model_id} on {device} with dtype {dtype}...")
        download_progress["status"] = "downloading"
        download_progress["description"] = f"Initializing model download ({size} {model_type})..."
        download_progress["progress"] = 0.0
        
        try:
            model = await asyncio.to_thread(_load_model_sync, expected_model_id, device, dtype)
            current_model_id = expected_model_id
            download_progress["status"] = "ready"
            download_progress["description"] = "Model loaded successfully."
            download_progress["progress"] = 100.0
            return model
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"Failed to load model: {e}")
            model = None
            current_model_id = None
            download_progress["status"] = "error"
            download_progress["description"] = f"Failed to load: {str(e)}"
            raise RuntimeError(f"Failed to load model: {e}")

app = FastAPI(lifespan=lifespan)

@app.get("/api/progress")
async def stream_progress(request: Request):
    async def event_generator():
        while True:
            if await request.is_disconnected():
                break

            yield f"data: {json.dumps(download_progress)}\n\n"

            if download_progress["status"] in ["ready", "error"]:
                break
            else:
                await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")

# Mount statics
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
if not os.path.exists(os.path.join(static_dir, "index.html")):
    with open(os.path.join(static_dir, "index.html"), "w") as f:
        f.write("<html><body><h1>Local TTS Studio Placeholder</h1></body></html>")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    return FileResponse(os.path.join(static_dir, "index.html"))

@app.get("/api/profiles")
def get_profiles():
    """List all saved voice profiles."""
    return load_profiles()

@app.post("/api/profiles")
async def create_profile(
    name: str = Form(...),
    ref_text: str = Form(...),
    ref_audio: UploadFile = File(...)
):
    """Save a new voice profile."""
    profile_id = str(uuid.uuid4())
    safe_filename = os.path.basename(ref_audio.filename) if ref_audio.filename else "audio.wav"
    audio_path = os.path.join(PROFILES_DIR, f"{profile_id}_{safe_filename}")
    
    with open(audio_path, "wb") as f:
        f.write(await ref_audio.read())
        
    profiles = load_profiles()
    profiles.append({
        "id": profile_id,
        "name": name,
        "ref_text": ref_text,
        "audio_path": audio_path
    })
    save_profiles(profiles)
    
    return {"message": "Profile created successfully", "id": profile_id}

@app.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: str):
    """Delete a saved voice profile."""
    if profile_id == BUILTIN_PROFILE_ID:
        raise HTTPException(status_code=403, detail="Cannot delete the built-in voice profile")
    
    profiles = load_profiles()
    profile = next((p for p in profiles if p["id"] == profile_id), None)
    
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
        
    audio_path = os.path.realpath(profile["audio_path"])
    if audio_path.startswith(os.path.realpath(PROFILES_DIR)) and os.path.exists(audio_path):
        os.remove(audio_path)
        
    profiles = [p for p in profiles if p["id"] != profile_id]
    save_profiles(profiles)
    
    return {"message": "Profile deleted successfully"}

@app.post("/api/generate")
async def generate_audio(
    text: str = Form(...),
    language: str = Form("English"),
    model_size: str = Form("1.7B"),
    model_type: str = Form("CustomVoice"),
    speaker: str = Form("Vivian"),
    voice_design_prompt: str = Form(None),
    ref_text: str = Form(None),
    ref_audio: UploadFile = File(None),
    profile_id: str = Form(None)
):
    if model_size not in VALID_MODEL_SIZES:
        raise HTTPException(status_code=400, detail=f"Invalid model_size. Must be one of: {', '.join(VALID_MODEL_SIZES)}")
    if model_type not in VALID_MODEL_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid model_type. Must be one of: {', '.join(VALID_MODEL_TYPES)}")

    try:
        tts_model = await get_tts_model(model_size, model_type)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    try:
        # Generate speech based on requested model type
        if model_type == "CustomVoice":
            wavs, sr = await asyncio.to_thread(
                tts_model.generate_custom_voice,
                text=text,
                language=language,
                speaker=speaker,
                temperature=0.3,
                repetition_penalty=1.1,
                top_p=0.8,
                subtalker_temperature=0.3
            )
        elif model_type == "VoiceDesign":
            if not voice_design_prompt:
                raise HTTPException(status_code=400, detail="voice_design_prompt is required for VoiceDesign models.")
            wavs, sr = await asyncio.to_thread(
                tts_model.generate_voice_design,
                text=text,
                language=language,
                instruct=voice_design_prompt,
                temperature=0.3,
                repetition_penalty=1.1,
                top_p=0.8,
                subtalker_temperature=0.3
            )
        elif model_type == "Base":
            if profile_id:
                # Load from saved profile
                profiles = load_profiles()
                profile = next((p for p in profiles if p["id"] == profile_id), None)
                if not profile:
                    raise HTTPException(status_code=404, detail="Profile not found")

                temp_audio_path = profile["audio_path"]
                actual_ref_text = profile["ref_text"]
                cleanup_audio = False
            else:
                # Use uploaded ad-hoc files
                if not ref_text or not ref_audio:
                    raise HTTPException(status_code=400, detail="ref_text and ref_audio (or profile_id) are required for Voice Cloning in Base models.")
                safe_name = os.path.basename(ref_audio.filename) if ref_audio.filename else "upload.wav"
                temp_audio_path = os.path.join(DATA_DIR, f"{uuid.uuid4()}_{safe_name}")
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(temp_audio_path, "wb") as f:
                    f.write(await ref_audio.read())
                actual_ref_text = ref_text
                cleanup_audio = True

            wavs, sr = await asyncio.to_thread(
                tts_model.generate_voice_clone,
                text=text,
                language=language,
                ref_audio=temp_audio_path,
                ref_text=actual_ref_text,
                temperature=0.3,
                repetition_penalty=1.1,
                top_p=0.8,
                subtalker_temperature=0.3
            )

            # Cleanup temp file if it was a temporary upload
            if cleanup_audio and os.path.exists(temp_audio_path):
                os.remove(temp_audio_path)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported model_type: {model_type}")

        # Stream audio directly from memory — no temp file needed
        audio_data = wavs[0]
        buffer = io.BytesIO()
        sf.write(buffer, audio_data, sr, format="WAV")
        buffer.seek(0)
        return StreamingResponse(buffer, media_type="audio/wav", headers={"Content-Disposition": "attachment; filename=generated.wav"})

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/merge")
async def merge_audio(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="No files provided")
        
    try:
        # Read all uploads into memory first (async), then merge in a thread
        file_contents = []
        for file in files:
            file_contents.append(await file.read())

        def _merge_sync():
            combined = AudioSegment.empty()
            silence = AudioSegment.silent(duration=1000)
            for idx, content in enumerate(file_contents):
                temp_in = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
                temp_in.write(content)
                temp_in.flush()
                temp_in.close()
                segment = AudioSegment.from_wav(temp_in.name)
                if idx > 0:
                    combined += silence
                combined += segment
                os.unlink(temp_in.name)
            temp_out = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
            temp_out.close()
            combined.export(temp_out.name, format="wav")
            return temp_out.name

        out_path = await asyncio.to_thread(_merge_sync)

        return FileResponse(
            out_path,
            media_type="audio/wav",
            filename="merged_audio.wav",
            background=BackgroundTask(os.unlink, out_path)
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to merge audio: {str(e)}")

@app.post("/api/treat")
async def treat_audio(
    audio_file: UploadFile = File(...),
    treatment_type: str = Form(...)
):
    """
    Apply ffmpeg audio enhancements to an uploaded audio file and return the processed file.
    """
    if not audio_file:
        raise HTTPException(status_code=400, detail="No audio file provided.")
        
    valid_treatments = ["podcast", "warmth", "clear"]
    if treatment_type not in valid_treatments:
        raise HTTPException(status_code=400, detail=f"Invalid treatment type. Must be one of: {', '.join(valid_treatments)}")

    try:
        # Save the uploaded file to a temporary location
        content = await audio_file.read()
        temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_input.write(content)
        temp_input.flush()
        temp_input.close()

        # Define output file
        temp_output = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
        temp_output.close()

        # Determine the ffmpeg filter chain based on treatment_type
        filter_chain = ""
        if treatment_type == "podcast":
            # Loudness normalization only — zero coloration, just standardized level
            filter_chain = "loudnorm=I=-16:TP=-1.5:LRA=11"
        elif treatment_type == "warmth":
            # Strong low shelf (+6dB at 200Hz) for noticeably warm, full-bodied sound
            filter_chain = "bass=g=6:f=200,loudnorm=I=-16:TP=-1.5:LRA=11"
        elif treatment_type == "clear":
            # Strong high shelf (+7dB at 2kHz) for noticeably crisp, airy, bright sound
            filter_chain = "treble=g=7:f=2000,loudnorm=I=-16:TP=-1.5:LRA=11"

        # Execute ffmpeg
        ffmpeg_cmd = "ffmpeg"
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            ffmpeg_cmd = os.path.join(sys._MEIPASS, 'ffmpeg')
            
        command = [
            ffmpeg_cmd,
            "-y",  # Overwrite output file if it exists
            "-i", temp_input.name,
            "-af", filter_chain,
            temp_output.name
        ]
        
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            print(f"ffmpeg error: {stderr.decode()}")
            raise RuntimeError(f"ffmpeg processing failed")

        # Clean up input file immediately since processing is done
        os.unlink(temp_input.name)

        return FileResponse(
            temp_output.name,
            media_type="audio/wav",
            filename=f"{treatment_type}_treated.wav",
            background=BackgroundTask(os.unlink, temp_output.name)
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        # Ensure input file is cleaned up on error if it was created
        if 'temp_input' in locals() and os.path.exists(temp_input.name):
            os.unlink(temp_input.name)
        raise HTTPException(status_code=500, detail=f"Failed to treat audio: {str(e)}")

@app.get("/api/check_update")
async def check_update():
    try:
        response = await asyncio.to_thread(requests.get, f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest")
        response.raise_for_status()
        data = response.json()
        
        latest_version = data.get("tag_name", "").lstrip("v")
        
        if latest_version and latest_version > APP_VERSION:
            assets = data.get("assets", [])
            download_url = None
            for asset in assets:
                if asset["name"].endswith(".zip"):
                    download_url = asset["browser_download_url"]
                    break
            
            if download_url:
                return {"update_available": True, "latest_version": latest_version, "download_url": download_url}
                
    except Exception as e:
        print(f"Update check failed: {e}")
        
    return {"update_available": False}

@app.post("/api/do_update")
async def do_update(download_url: str = Form(...)):
    if not getattr(sys, 'frozen', False):
        raise HTTPException(status_code=400, detail="Cannot perform OTA update on unpacked source code. Must be a PyInstaller build.")

    try:
        import shutil
        app_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(sys.executable))))
        if not app_path.endswith(".app"):
            raise HTTPException(status_code=400, detail="Current executable is not inside a standard macOS .app bundle structure.")

        # Download the zip
        temp_dir = tempfile.mkdtemp(prefix="tts_update_")
        zip_path = os.path.join(temp_dir, "update.zip")
        
        def _download_and_extract():
            r = requests.get(download_url, stream=True)
            r.raise_for_status()
            with open(zip_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192): 
                    f.write(chunk)
            # Extact zip
            import zipfile
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_dir)
        
        await asyncio.to_thread(_download_and_extract)
        
        # Look for the new .app bundle
        extracted_app_path = None
        for item in os.listdir(temp_dir):
            if item.endswith(".app"):
                extracted_app_path = os.path.join(temp_dir, item)
                break
                
        if not extracted_app_path:
            raise Exception("No .app bundle found in the downloaded zip.")

        # Create bash script to replace the app
        script_path = os.path.join(temp_dir, "update.sh")
        with open(script_path, "w") as f:
            f.write(f'''#!/bin/bash
sleep 4
rm -rf "{app_path}"
mv "{extracted_app_path}" "{app_path}"
open "{app_path}"
rm -rf "{temp_dir}"
''')
        os.chmod(script_path, 0o755)
        
        # Run it detached
        subprocess.Popen(["/bin/bash", script_path], start_new_session=True)
        
        # Kill the current process right away to let script do the replacement
        async def _kill_soon():
            await asyncio.sleep(2.0)
            os._exit(0)
        asyncio.create_task(_kill_soon())
        
        return {"status": "success", "message": "Update initiated. Restarting..."}

    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
