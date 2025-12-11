# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# -------------------------------------------------------------------------
from __future__ import annotations
import os
import sys
import argparse
import asyncio
import base64
from datetime import datetime
import logging
import queue
import signal
from typing import Union, Optional, TYPE_CHECKING, cast

from azure.core.credentials import AzureKeyCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import AzureCliCredential, DefaultAzureCredential

from azure.ai.voicelive.aio import connect
from azure.ai.voicelive.models import (
    AudioEchoCancellation,
    AudioNoiseReduction,
    AzureStandardVoice,
    InputAudioFormat,
    Modality,
    OutputAudioFormat,
    RequestSession,
    ServerEventType,
    ServerVad
)
from dotenv import load_dotenv
import threading
import pyaudio
import numpy as np
import gui

if TYPE_CHECKING:
    # Only needed for type checking; avoids runtime import issues
    from azure.ai.voicelive.aio import VoiceLiveConnection

## Change to the directory where this script is located
os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Environment variable loading
load_dotenv('./.env', override=True)

# Set up logging
## Add folder for logging
if not os.path.exists('logs'):
    os.makedirs('logs')

## Add timestamp for logfiles
timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

## Set up logging
logging.basicConfig(
    filename=f'logs/{timestamp}_voicelive.log',
    filemode="w",
    format='%(asctime)s:%(name)s:%(levelname)s:%(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class AudioProcessor:
    """
    Handles real-time audio capture and playback for the voice assistant.

    Threading Architecture:
    - Main thread: Event loop and UI
    - Capture thread: PyAudio input stream reading
    - Send thread: Async audio data transmission to VoiceLive
    - Playback thread: PyAudio output stream writing
    """
    
    loop: asyncio.AbstractEventLoop
    
    class AudioPlaybackPacket:
        """Represents a packet that can be sent to the audio playback queue."""
        def __init__(self, seq_num: int, data: Optional[bytes]):
            self.seq_num = seq_num
            self.data = data

    def __init__(self, connection):
        self.connection = connection
        self.audio = pyaudio.PyAudio()

        # Audio configuration - PCM16, 24kHz, mono as specified
        self.format = pyaudio.paInt16
        self.channels = 1
        self.rate = 24000
        self.chunk_size = 1200 # 50ms

        # Capture and playback state
        self.input_stream = None

        self.playback_queue: queue.Queue[AudioProcessor.AudioPlaybackPacket] = queue.Queue()
        self.playback_base = 0
        self.next_seq_num = 0
        self.output_stream: Optional[pyaudio.Stream] = None

        logger.info("AudioProcessor initialized with 24kHz PCM16 mono audio")

    def start_capture(self):
        """Start capturing audio from microphone."""
        def _capture_callback(
            in_data,      # data
            _frame_count,  # number of frames
            _time_info,    # dictionary
            _status_flags):
            """Audio capture thread - runs in background."""
            # compute RMS level for visualization (int16 PCM)
            try:
                arr = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
                if arr.size:
                    rms = float(np.sqrt((arr * arr).mean()) / 32768.0)
                else:
                    rms = 0.0
                try:
                    gui.update_audio_level(rms, "user")
                except Exception:
                    pass
            except Exception:
                pass

            # If GUI mute is active, do not send audio to the service
            try:
                if gui.is_muted():
                    return (None, pyaudio.paContinue)
            except Exception:
                # if GUI not available, continue sending
                pass

            audio_base64 = base64.b64encode(in_data).decode("utf-8")
            asyncio.run_coroutine_threadsafe(
                self.connection.input_audio_buffer.append(audio=audio_base64), self.loop
            )
            return (None, pyaudio.paContinue)

        if self.input_stream:
            return

        # Store the current event loop for use in threads
        self.loop = asyncio.get_event_loop()

        try:
            self.input_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                input=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=_capture_callback,
            )
            logger.info("Started audio capture")

        except Exception:
            logger.exception("Failed to start audio capture")
            raise

    def start_playback(self):
        """Initialize audio playback system."""
        if self.output_stream:
            return

        remaining = bytes()
        def _playback_callback(
            _in_data,
            frame_count,  # number of frames
            _time_info,
            _status_flags):

            nonlocal remaining
            frame_count *= pyaudio.get_sample_size(pyaudio.paInt16)

            out = remaining[:frame_count]
            remaining = remaining[frame_count:]

            while len(out) < frame_count:
                try:
                    packet = self.playback_queue.get_nowait()
                except queue.Empty:
                    out = out + bytes(frame_count - len(out))
                    continue
                except Exception:
                    logger.exception("Error in audio playback")
                    raise

                if not packet or not packet.data:
                    # None packet indicates end of stream
                    logger.info("End of playback queue.")
                    break

                if packet.seq_num < self.playback_base:
                    # skip requested
                    # ignore skipped packet and clear remaining
                    if len(remaining) > 0:
                        remaining = bytes()
                    continue

                num_to_take = frame_count - len(out)
                out = out + packet.data[:num_to_take]
                remaining = packet.data[num_to_take:]

            if len(out) >= frame_count:
                return (out, pyaudio.paContinue)
            else:
                return (out, pyaudio.paComplete)

        try:
            self.output_stream = self.audio.open(
                format=self.format,
                channels=self.channels,
                rate=self.rate,
                output=True,
                frames_per_buffer=self.chunk_size,
                stream_callback=_playback_callback
            )
            logger.info("Audio playback system ready")
        except Exception:
            logger.exception("Failed to initialize audio playback")
            raise

    def _get_and_increase_seq_num(self):
        seq = self.next_seq_num
        self.next_seq_num += 1
        return seq

    def queue_audio(self, audio_data: Optional[bytes]) -> None:
        """Queue audio data for playback."""
        self.playback_queue.put(
            AudioProcessor.AudioPlaybackPacket(
                seq_num=self._get_and_increase_seq_num(),
                data=audio_data))

    def skip_pending_audio(self):
        """Skip current audio in playback queue."""
        self.playback_base = self._get_and_increase_seq_num()

    def shutdown(self):
        """Clean up audio resources."""
        if self.input_stream:
            self.input_stream.stop_stream()
            self.input_stream.close()
            self.input_stream = None

        logger.info("Stopped audio capture")

        # Inform thread to complete
        if self.output_stream:
            self.skip_pending_audio()
            self.queue_audio(None)
            self.output_stream.stop_stream()
            self.output_stream.close()
            self.output_stream = None

        logger.info("Stopped audio playback")

        if self.audio:
            self.audio.terminate()

        logger.info("Audio processor cleaned up")

    def stop_capture(self):
        """Stop only the input capture stream (leave playback intact)."""
        try:
            if self.input_stream:
                try:
                    self.input_stream.stop_stream()
                except Exception:
                    pass
                try:
                    self.input_stream.close()
                except Exception:
                    pass
                self.input_stream = None
                logger.info("Audio capture stopped")
        except Exception:
            logger.exception("Failed to stop audio capture")

class BasicVoiceAssistant:
    """Basic voice assistant implementing the VoiceLive SDK patterns."""

    def __init__(
        self,
        endpoint: str,
        credential: Union[AzureKeyCredential, AsyncTokenCredential],
        model: str,
        voice: str,
        instructions: str,
    ):

        self.endpoint = endpoint
        self.credential = credential
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.connection: Optional["VoiceLiveConnection"] = None
        self.audio_processor: Optional[AudioProcessor] = None
        self.session_ready = False
        self._active_response = False
        self._response_api_done = False

    async def start(self):
        """Start the voice assistant session."""
        try:
            logger.info("Connecting to VoiceLive API with model %s", self.model)

            # Connect to VoiceLive WebSocket API
            async with connect(
                endpoint=self.endpoint,
                credential=self.credential,
                model=self.model,
            ) as connection:
                conn = connection
                self.connection = conn

                # Initialize audio processor
                ap = AudioProcessor(conn)
                self.audio_processor = ap

                # Configure session for voice conversation
                await self._setup_session()

                # Start audio systems
                ap.start_playback()

                # Start GUI for visualizing interactions
                try:
                    gui.start_gui()
                except Exception:
                    logger.exception("Failed to start GUI")
                # register to GUI selections (planets / rockets / atro)
                try:
                    gui.register_selection_callback(self._on_gui_selection)
                except Exception:
                    logger.debug("GUI callback registration skipped")

                # Start capturing microphone immediately (user requested mic open at program start)
                try:
                    if ap:
                        ap.start_capture()
                        logger.info("Audio capture started at program start")
                except Exception:
                    logger.exception("Failed to start capture at program start")

                # Try to create an initial assistant response so the IA starts the conversation
                try:
                    initial_prompt = os.environ.get(
                        "AZURE_VOICELIVE_INITIAL_PROMPT",
                        "Hola, soy AstroGuía. ¿Quieres explorar planetas o estrellas hoy?"
                    )
                    # Try common method names on the connection's response helper.
                    created = False
                    if hasattr(conn, 'response'):
                        resp_obj = getattr(conn, 'response')
                        if hasattr(resp_obj, 'create'):
                            try:
                                await resp_obj.create(text=initial_prompt)
                                created = True
                            except TypeError:
                                # try alternative signature
                                try:
                                    await resp_obj.create(prompt=initial_prompt)
                                    created = True
                                except Exception:
                                    pass
                        elif hasattr(resp_obj, 'start'):
                            try:
                                await resp_obj.start(text=initial_prompt)
                                created = True
                            except Exception:
                                pass
                    # Fallback: try plural attribute
                    if not created and hasattr(conn, 'responses'):
                        resp_obj = getattr(conn, 'responses')
                        if hasattr(resp_obj, 'create'):
                            try:
                                await resp_obj.create(text=initial_prompt)
                                created = True
                            except Exception:
                                pass

                    if created:
                        logger.info("Requested initial assistant response: %s", initial_prompt)
                    else:
                        logger.debug("Could not find response.create/start on connection to request initial assistant response")
                except Exception:
                    logger.exception("Failed to request initial assistant response")

                logger.info("Voice assistant ready! Start speaking...")
                print("\n" + "=" * 60)
                print("🎤 VOICE ASSISTANT READY")
                print("Start speaking to begin conversation")
                print("Press Ctrl+C to exit")
                print("=" * 60 + "\n")

                # Process events
                await self._process_events()
        finally:
            if self.audio_processor:
                self.audio_processor.shutdown()

    async def _setup_session(self):
        """Configure the VoiceLive session for audio conversation."""
        logger.info("Setting up voice conversation session...")

        # Create voice configuration
        voice_config: Union[AzureStandardVoice, str]
        if self.voice.startswith("en-US-") or self.voice.startswith("en-CA-") or "-" in self.voice:
            # Azure voice
            voice_config = AzureStandardVoice(name=self.voice)
        else:
            # OpenAI voice (alloy, echo, fable, onyx, nova, shimmer)
            voice_config = self.voice

        # Create turn detection configuration
        turn_detection_config = ServerVad(
            threshold=0.5,
            prefix_padding_ms=300,
            silence_duration_ms=500)

        # Create session configuration
        session_config = RequestSession(
            modalities=[Modality.TEXT, Modality.AUDIO],
            instructions=self.instructions,
            voice=voice_config,
            input_audio_format=InputAudioFormat.PCM16,
            output_audio_format=OutputAudioFormat.PCM16,
            turn_detection=turn_detection_config,
            input_audio_echo_cancellation=AudioEchoCancellation(),
            input_audio_noise_reduction=AudioNoiseReduction(type="azure_deep_noise_suppression"),
        )

        conn = self.connection
        assert conn is not None, "Connection must be established before setting up session"
        await conn.session.update(session=session_config)

        logger.info("Session configuration sent")

    async def _process_events(self):
        """Process events from the VoiceLive connection."""
        try:
            conn = self.connection
            assert conn is not None, "Connection must be established before processing events"
            async for event in conn:
                await self._handle_event(event)
        except Exception:
            logger.exception("Error processing events")
            raise

    async def _handle_event(self, event):
        """Handle different types of events from VoiceLive."""
        logger.debug("Received event: %s", event.type)
        ap = self.audio_processor
        conn = self.connection
        assert ap is not None, "AudioProcessor must be initialized"
        assert conn is not None, "Connection must be established"

        if event.type == ServerEventType.SESSION_UPDATED:
            logger.info("Session ready: %s", event.session.id)
            self.session_ready = True

            # Session ready. Do not start capture automatically — capture will
            # be started when the user selects the AtroBot option in the GUI.

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STARTED:
            logger.info("User started speaking - stopping playback")
            print("🎤 Listening...")

            ap.skip_pending_audio()

            try:
                gui.display_message("Usuario empezó a hablar...", "user")
            except Exception:
                pass
            try:
                gui.set_active_source("user")
            except Exception:
                pass

            # Only cancel if response is active and not already done
            if self._active_response and not self._response_api_done:
                try:
                    await conn.response.cancel()
                    logger.debug("Cancelled in-progress response due to barge-in")
                except Exception as e:
                    if "no active response" in str(e).lower():
                        logger.debug("Cancel ignored - response already completed")
                    else:
                        logger.warning("Cancel failed: %s", e)

        elif event.type == ServerEventType.INPUT_AUDIO_BUFFER_SPEECH_STOPPED:
            logger.info("🎤 User stopped speaking")
            print("🤔 Processing...")
            try:
                gui.display_message("Usuario dejó de hablar", "user")
            except Exception:
                pass

        elif event.type == ServerEventType.RESPONSE_CREATED:
            logger.info("🤖 Assistant response created")
            self._active_response = True
            self._response_api_done = False
            try:
                gui.display_message("Asistente: generando respuesta...", "ai")
            except Exception:
                pass
            try:
                gui.set_active_source("ai")
            except Exception:
                pass
            try:
                gui.set_ai_speaking(True)
            except Exception:
                pass

        elif event.type == ServerEventType.RESPONSE_AUDIO_DELTA:
            logger.debug("Received audio delta")
            # Queue audio for playback
            ap.queue_audio(event.delta)
            # Compute level for visualization (optional, GUI may ignore)
            try:
                samples = np.frombuffer(event.delta, dtype=np.int16).astype(np.float32)
                if samples.size:
                    rms = float(np.sqrt((samples * samples).mean()) / 32768.0)
                else:
                    rms = 0.0

                try:
                    fft = np.abs(np.fft.rfft(samples))
                    bins = 32
                    chunk = max(1, len(fft) // bins)
                    mags = [float(np.max(fft[i * chunk : (i + 1) * chunk])) for i in range(bins)]
                    mags = np.array(mags)
                    if mags.max() > 0:
                        mags = mags / mags.max()
                    gui.update_audio_level(mags, "ai")
                except Exception:
                    gui.update_audio_level(rms, "ai")
            except Exception:
                pass

        elif event.type == ServerEventType.RESPONSE_AUDIO_DONE:
            logger.info("🤖 Assistant finished speaking")
            print("🎤 Ready for next input...")
            try:
                gui.display_message("Asistente terminó de hablar", "ai")
            except Exception:
                pass
            try:
                gui.set_ai_speaking(False)
            except Exception:
                pass

        elif event.type == ServerEventType.RESPONSE_DONE:
            logger.info("✅ Response complete")
            self._active_response = False
            self._response_api_done = True
            try:
                gui.display_message("Respuesta completa", "ai")
            except Exception:
                pass
            try:
                gui.set_ai_speaking(False)
            except Exception:
                pass

        elif event.type == ServerEventType.ERROR:
            msg = event.error.message
            if "Cancellation failed: no active response" in msg:
                logger.debug("Benign cancellation error: %s", msg)
            else:
                logger.error("❌ VoiceLive error: %s", msg)
                print(f"Error: {msg}")

        elif event.type == ServerEventType.CONVERSATION_ITEM_CREATED:
            logger.debug("Conversation item created: %s", event.item.id)

        else:
            logger.debug("Unhandled event type: %s", event.type)

    def _on_gui_selection(self, selection: str):
        """Callback from GUI when user selects an option."""
        try:
            logger.info("GUI selection: %s", selection)
            print(f"Seleccionado desde GUI: {selection}")
            if selection == 'atrobot':
                # user chose to talk to the IA
                try:
                    gui.set_active_source('ai')
                except Exception:
                    pass
                # start physical capture if session ready
                try:
                    if self.audio_processor and self.session_ready:
                        self.audio_processor.start_capture()
                        logger.info("Audio capture started due to GUI selection (AtroBot)")
                except Exception:
                    logger.exception("Failed to start capture on GUI selection")
                # optionally notify user in console
                print("Interacción con la IA activada. Habla cuando quieras...")
            elif selection == 'planetas':
                print("Has seleccionado Planetas (modo info).")
                try:
                    if self.audio_processor:
                        self.audio_processor.stop_capture()
                except Exception:
                    pass
            elif selection == 'cohetes':
                print("Has seleccionado Cohetes (modo info).")
                try:
                    if self.audio_processor:
                        self.audio_processor.stop_capture()
                except Exception:
                    pass
        except Exception:
            logger.exception("Error handling GUI selection")

    # _on_gui_selection moved below after event handling


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Basic Voice Assistant using Azure VoiceLive SDK",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument(
        "--api-key",
        help="Azure VoiceLive API key. If not provided, will use AZURE_VOICELIVE_API_KEY environment variable.",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_API_KEY"),
    )

    parser.add_argument(
        "--endpoint",
        help="Azure VoiceLive endpoint",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_ENDPOINT", "https://astrobot-resource.services.ai.azure.com/"),
    )

    parser.add_argument(
        "--model",
        help="VoiceLive model to use",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_MODEL", "gpt-realtime"),
    )

    parser.add_argument(
        "--voice",
        help="Voice to use for the assistant. E.g. alloy, echo, fable, en-US-AvaNeural, en-US-GuyNeural",
        type=str,
        default=os.environ.get("AZURE_VOICELIVE_VOICE", "en-US-Ava:DragonHDLatestNeural"),
    )

    parser.add_argument(
        "--instructions",
        help="System instructions for the AI assistant",
        type=str,
        default=os.environ.get(
            "AZURE_VOICELIVE_INSTRUCTIONS",
            "You are a helpful AI assistant. Respond naturally and conversationally. "
            "Keep your responses concise but engaging.",
        ),
    )

    parser.add_argument(
        "--use-token-credential", help="Use Azure token credential instead of API key", action="store_true", default=False
    )

    parser.add_argument("--verbose", help="Enable verbose logging", action="store_true")

    return parser.parse_args()


def main():
    """Main function."""
    args = parse_arguments()

    # Set logging level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate credentials
    if not args.api_key and not args.use_token_credential:
        print("❌ Error: No authentication provided")
        print("Please provide an API key using --api-key or set AZURE_VOICELIVE_API_KEY environment variable,")
        print("or use --use-token-credential for Azure authentication.")
        sys.exit(1)

    # Create client with appropriate credential
    credential: Union[AzureKeyCredential, AsyncTokenCredential]
    if args.use_token_credential:
        credential = AzureCliCredential()  # or DefaultAzureCredential() if needed
        logger.info("Using Azure token credential")
    else:
        credential = AzureKeyCredential(args.api_key)
        logger.info("Using API key credential")

    # Create and start voice assistant
    assistant = BasicVoiceAssistant(
        endpoint=args.endpoint,
        credential=credential,
        model=args.model,
        voice=args.voice,
        instructions=args.instructions,
    )

    def _start_status_console(assistant_obj: BasicVoiceAssistant):
        """Start a background thread that reads simple status commands from stdin.

        Commands:
        - `status`: prints whether the input capture stream is active and GUI mute state.
        - `quit`/`exit`: requests program shutdown (sends SIGINT).
        """
        def _loop():
            try:
                while True:
                    try:
                        line = input()
                    except EOFError:
                        break
                    if not line:
                        continue
                    cmd = line.strip().lower()
                    if cmd == 'status':
                        ap = assistant_obj.audio_processor
                        if ap is None:
                            print("AudioProcessor no inicializado todavía.")
                        else:
                            inp = getattr(ap, 'input_stream', None)
                            try:
                                active = bool(inp is not None and getattr(inp, 'is_active', lambda: False)())
                                print(f"Micrófono activo: {active}")
                            except Exception:
                                print(f"Micrófono activo: {inp is not None}")
                        try:
                            muted = gui.is_muted()
                            print(f"GUI muted: {muted}")
                        except Exception:
                            pass
                    elif cmd in ('quit', 'exit'):
                        print("Solicitando cierre de la aplicación...")
                        try:
                            import os, signal
                            os.kill(os.getpid(), signal.SIGINT)
                        except Exception:
                            pass
                        break
            except Exception:
                pass

        t = threading.Thread(target=_loop, daemon=True)
        t.start()

    # Start status console helper so you can type `status` in this terminal
    try:
        _start_status_console(assistant)
    except Exception:
        pass

    # Setup signal handlers for graceful shutdown
    def signal_handler(_sig, _frame):
        logger.info("Received shutdown signal")
        raise KeyboardInterrupt()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start the assistant
    try:
        asyncio.run(assistant.start())
    except KeyboardInterrupt:
        print("\n👋 Voice assistant shut down. Goodbye!")
    except Exception as e:
        print("Fatal Error: ", e)

if __name__ == "__main__":
    # Check audio system
    try:
        p = pyaudio.PyAudio()
        # Check for input devices
        input_devices = [
            i
            for i in range(p.get_device_count())
            if cast(Union[int, float], p.get_device_info_by_index(i).get("maxInputChannels", 0) or 0) > 0
        ]
        # Check for output devices
        output_devices = [
            i
            for i in range(p.get_device_count())
            if cast(Union[int, float], p.get_device_info_by_index(i).get("maxOutputChannels", 0) or 0) > 0
        ]
        p.terminate()

        if not input_devices:
            print("❌ No audio input devices found. Please check your microphone.")
            sys.exit(1)
        if not output_devices:
            print("❌ No audio output devices found. Please check your speakers.")
            sys.exit(1)

    except Exception as e:
        print(f"❌ Audio system check failed: {e}")
        sys.exit(1)

    print("🎙️  Basic Voice Assistant with Azure VoiceLive SDK")
    print("=" * 50)

    # Run the assistant
    main()
