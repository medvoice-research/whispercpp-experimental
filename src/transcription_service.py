import json
import logging
import os
import subprocess
from pathlib import Path

# All of these imports are used in method signatures

import ffmpeg

# Import configuration
from config import TEMP_UPLOAD_DIR as DEFAULT_TEMP_DIR

# Import speaker diarization service (if available)
try:
    from speaker_diarization import SpeakerDiarizationService

    PYANNOTE_AVAILABLE = True
except ImportError:
    PYANNOTE_AVAILABLE = False
    logging.warning("pyannote.audio not available; advanced speaker diarization disabled")

logger = logging.getLogger(__name__)


class TranscriptionService:
    def __init__(self, model_path, whisper_bin="whisper-cli", temp_dir=None, hf_token=None):
        """
        Initialize the transcription service

        Args:
            model_path: Path to the whisper model file
            whisper_bin: Path to the whisper-cli binary
            temp_dir: Directory to store temporary files
            hf_token: Hugging Face API token for accessing pyannote models
        """
        self.model_path = Path(model_path)
        self.whisper_bin = whisper_bin
        self.temp_dir = Path(temp_dir) if temp_dir is not None else DEFAULT_TEMP_DIR
        self.hf_token = hf_token

        # Initialize speaker diarization service if available
        self.diarization_service = None
        if PYANNOTE_AVAILABLE and hf_token:
            try:
                self.diarization_service = SpeakerDiarizationService(hf_token=hf_token)
                logging.info("Initialized pyannote speaker diarization service")
            except Exception as e:
                logging.error(f"Failed to initialize speaker diarization service: {e}")

        if not self.temp_dir.exists():
            self.temp_dir.mkdir(parents=True)

        if not self.model_path.exists():
            raise FileNotFoundError(f"Model file not found at {model_path}")

    def convert_audio_to_wav(self, audio_path):
        """Convert audio file to 16-bit WAV format required by whisper.cpp"""
        output_path = self.temp_dir / f"{Path(audio_path).stem}_converted.wav"

        try:
            ffmpeg.input(audio_path).output(
                str(output_path), acodec="pcm_s16le", ar=16000, ac=1  # 16-bit PCM  # 16 kHz  # mono
            ).run(quiet=True, overwrite_output=True)

            logger.info(f"Converted {audio_path} to {output_path}")
            return output_path
        except ffmpeg.Error as e:
            logger.error(f"Error converting audio: {e.stderr.decode() if e.stderr else str(e)}")
            raise

    def transcribe(
        self,
        audio_path,
        enable_diarization=False,
        num_speakers=None,
        min_speakers=None,
        max_speakers=None,
        language="auto",
    ):
        """
        Transcribe audio file using whisper.cpp

        Args:
            audio_path: Path to the audio file
            enable_diarization: Whether to enable speaker diarization (requires compatible model)

        Returns:
            A dictionary with the transcription results
        """
        wav_path = None
        try:
            wav_path = self.convert_audio_to_wav(audio_path)

            # Prepare command
            cmd = [
                self.whisper_bin,
                "-m",
                str(self.model_path),
                "-f",
                str(wav_path),
                "-oj",  # Output JSON
                "-l",
                language,  # Use specified language or auto-detect
            ]

            # Check if we should use whisper.cpp's built-in diarization
            use_whisper_diarization = enable_diarization and "tdrz" in str(self.model_path)
            use_pyannote_diarization = (
                enable_diarization
                and self.diarization_service is not None
                and not use_whisper_diarization
            )

            if use_whisper_diarization:
                # Add diarization parameter if model supports it (e.g. small.en-tdrz)
                cmd.append("-tdrz")
                logger.info("Using whisper.cpp built-in diarization (tinydiarize)")
            elif enable_diarization:
                if not self.diarization_service:
                    logger.warning(
                        "Diarization requested but neither tinydiarize nor pyannote are available. "
                        "Using regular transcription without diarization."
                    )

            cmd_str = " ".join(cmd)
            logger.info(f"Running command: {cmd_str}")

            try:
                result = subprocess.run(cmd, check=False, capture_output=True, text=True)
            except FileNotFoundError:
                logger.error(f"Error: whisper-cli not found at '{self.whisper_bin}'")
                error_msg = (
                    "whisper-cli not found. Please install whisper.cpp and ensure the binary "
                    "is in your PATH or set the WHISPER_BIN_PATH environment variable."
                )
                return {"error": error_msg}

            # Get the output
            output = result.stdout.strip()
            logger.debug(f"Raw output: {output}")

            if not output:
                logger.error("No output from transcription command")
                return {"error": "No output from transcription command: " + result.stderr}

            try:
                # Try to parse as JSON first (if -oj flag worked as expected)
                transcription_result = json.loads(output)  # type: ignore [assignment]

                # Apply pyannote diarization if requested and available
                if use_pyannote_diarization and self.diarization_service is not None:
                    logger.info("Applying pyannote speaker diarization")
                    try:
                        diarization_result = self.diarization_service.diarize(
                            audio_path=wav_path,
                            num_speakers=num_speakers,
                            min_speakers=min_speakers,
                            max_speakers=max_speakers,
                        )

                        # Align diarization with transcription segments
                        if "segments" in transcription_result:
                            # Convert segment timestamps to seconds if needed
                            for segment in transcription_result["segments"]:
                                if "start" not in segment and "t0" in segment:
                                    segment["start"] = segment["t0"]
                                if "end" not in segment and "t1" in segment:
                                    segment["end"] = segment["t1"]

                            # Add speaker labels to segments
                            transcription_result[
                                "segments"
                            ] = self.diarization_service.align_diarization_with_transcription(
                                diarization_result=diarization_result,
                                transcription_segments=transcription_result["segments"],
                            )

                            # Add diarization metadata
                            transcription_result["diarization"] = {
                                "num_speakers": diarization_result["num_speakers"],
                                "method": "pyannote",
                            }

                            # Format the full text with speaker labels for better readability
                            if "text" in transcription_result:
                                speaker_texts = []
                                for segment in transcription_result["segments"]:
                                    if "speaker" in segment and "text" in segment:
                                        speaker_texts.append(
                                            f"{segment['speaker']}: {segment['text']}"
                                        )

                                # Update the full text to include speaker information
                                transcription_result["text_with_speakers"] = "\n".join(
                                    speaker_texts
                                )
                    except Exception as e:
                        logger.error(f"Error during pyannote diarization: {e}")

                return transcription_result
            except json.JSONDecodeError:
                logger.info("Output is not JSON format, parsing as text")

                # Parse text output format: [timestamp --> timestamp] text
                segments = []
                full_text = ""
                lines = output.split("\n")

                for line in lines:
                    # Skip empty lines
                    if not line.strip():
                        continue

                    # Try to match the timestamp pattern [HH:MM:SS.mmm --> HH:MM:SS.mmm]
                    import re

                    match = re.match(r"\[(\d+:\d+:\d+\.\d+) --> (\d+:\d+:\d+\.\d+)\]\s+(.*)", line)
                    if match:
                        start_time = match.group(1)
                        end_time = match.group(2)
                        text = match.group(3)

                        # Convert HH:MM:SS.mmm to seconds
                        def time_to_seconds(time_str):
                            h, m, s = time_str.split(":")
                            return float(h) * 3600 + float(m) * 60 + float(s)

                        t0 = time_to_seconds(start_time)
                        t1 = time_to_seconds(end_time)

                        segments.append({"text": text.strip(), "t0": t0, "t1": t1})

                        full_text += text.strip() + " "

                # Create result in a similar format to the JSON output for consistency
                result = {
                    "text": full_text.strip(),
                    "segments": segments,
                    "language": "en",  # Assuming English by default
                }

                # Apply pyannote diarization if requested and available for non-JSON output too
                if enable_diarization and self.diarization_service is not None:
                    try:
                        logger.info("Applying pyannote speaker diarization to non-JSON output")
                        diarization_result = self.diarization_service.diarize(
                            audio_path=wav_path,
                            num_speakers=num_speakers,
                            min_speakers=min_speakers,
                            max_speakers=max_speakers,
                        )

                        # Add timestamp fields if not present
                        for segment in result["segments"]:
                            if "start" not in segment and "t0" in segment:
                                segment["start"] = segment["t0"]
                            if "end" not in segment and "t1" in segment:
                                segment["end"] = segment["t1"]

                        # Align diarization with segments
                        result[
                            "segments"
                        ] = self.diarization_service.align_diarization_with_transcription(
                            diarization_result=diarization_result,
                            transcription_segments=result["segments"],
                        )

                        # Add diarization metadata
                        result["diarization"] = {
                            "num_speakers": diarization_result["num_speakers"],
                            "method": "pyannote",
                        }

                        # Generate text with speakers
                        speaker_texts = []
                        for segment in result["segments"]:
                            if "speaker" in segment and "text" in segment:
                                speaker_texts.append(f"{segment['speaker']}: {segment['text']}")

                        if speaker_texts:
                            result["text_with_speakers"] = "\n".join(speaker_texts)

                        logger.info(
                            f"Added diarization to {len(result['segments'])} segments with "
                            f"{diarization_result['num_speakers']} speakers"
                        )

                    except Exception as e:
                        logger.error(f"Error applying pyannote diarization to non-JSON output: {e}")

                return result

        except subprocess.CalledProcessError as e:
            error_message = e.stderr if e.stderr else str(e)
            logger.error(f"Transcription failed: {error_message}")
            return {"error": f"Transcription failed: {error_message}"}
        finally:
            # Clean up temporary WAV file
            if wav_path and os.path.exists(wav_path):
                os.unlink(wav_path)

    def get_model_info(self):
        """Get information about the current model"""
        from models_data import MODEL_INFO

        model_name = self.model_path.stem.replace("ggml-", "")
        model_info = MODEL_INFO.get(model_name, {})

        is_diarization_capable = model_info.get("diarization", False) or "tdrz" in model_name

        result = {
            "model_name": model_name,
            "model_path": str(self.model_path),
            "supports_diarization": is_diarization_capable,
        }

        # Add additional info if available
        if model_info:
            result.update(
                {
                    "size_mb": model_info.get("size_mb"),
                    "multilingual": model_info.get("multilingual"),
                    "params": model_info.get("params"),
                    "quantized": model_info.get("quantized"),
                    "quantization_method": model_info.get("quantization"),
                }
            )

        return result
