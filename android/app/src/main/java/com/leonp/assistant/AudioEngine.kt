package com.leonp.assistant

import android.annotation.SuppressLint
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.AudioTrack
import android.media.MediaRecorder
import android.os.SystemClock
import android.util.Log
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import kotlin.math.max

class AudioEngine(
    private val onChunk: (ByteArray) -> Unit,
    private val onPlaybackDone: () -> Unit,
    private val onError: (String) -> Unit,
) {
    private sealed interface Item {
        class Audio(val bytes: ByteArray) : Item
        data object End : Item
    }

    private var record: AudioRecord? = null
    private var track: AudioTrack? = null
    private var micThread: Thread? = null
    private var playThread: Thread? = null
    private val queue = LinkedBlockingQueue<Item>()

    @Volatile
    private var micRunning = false

    @Volatile
    private var playRunning = false

    private var framesWritten = 0L

    fun start() {
        startPlayback()
        startMic()
    }

    @SuppressLint("MissingPermission")
    private fun startMic() {
        if (micRunning) return

        val minBuffer = AudioRecord.getMinBufferSize(
            INPUT_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val bufferSize = max(minBuffer, CHUNK_BYTES * 4)
        val recorder = try {
            buildRecorder(bufferSize)
        } catch (e: Exception) {
            onError("microphone unavailable: ${e.message}")
            null
        }

        if (recorder == null || recorder.state != AudioRecord.STATE_INITIALIZED) {
            recorder?.release()
            if (recorder != null) onError("microphone unavailable")
            return
        }

        try {
            recorder.startRecording()
        } catch (e: Exception) {
            recorder.release()
            onError("microphone unavailable: ${e.message}")
            return
        }

        record = recorder
        micRunning = true

        micThread = Thread({
            val buffer = ByteArray(CHUNK_BYTES)
            while (micRunning) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read == CHUNK_BYTES) {
                    onChunk(buffer.copyOf())
                } else if (read > 0) {
                    onChunk(buffer.copyOf(read))
                } else if (read < 0) {
                    onError("microphone read failed: $read")
                    break
                }
            }
        }, "mic").apply { start() }
    }

    @SuppressLint("MissingPermission")
    private fun buildRecorder(bufferSize: Int): AudioRecord? {
        val source = MediaRecorder.AudioSource.VOICE_RECOGNITION
        val format = AudioFormat.Builder()
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .setSampleRate(INPUT_RATE)
            .setChannelMask(AudioFormat.CHANNEL_IN_MONO)
            .build()

        val preferred = AudioRecord.Builder()
            .setAudioSource(source)
            .setAudioFormat(format)
            .setBufferSizeInBytes(bufferSize)
            .build()

        if (preferred.state == AudioRecord.STATE_INITIALIZED) {
            return preferred
        }
        preferred.release()

        return AudioRecord.Builder()
            .setAudioSource(MediaRecorder.AudioSource.MIC)
            .setAudioFormat(format)
            .setBufferSizeInBytes(bufferSize)
            .build()
    }

    private fun startPlayback() {
        if (playRunning) return

        val minBuffer = AudioTrack.getMinBufferSize(
            OUTPUT_RATE,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        val player = try {
            // USAGE_MEDIA (not USAGE_ASSISTANT): on this Echo Show the
            // assistant stream is effectively silent — MEDIA goes through the
            // same STREAM_MUSIC volume the user turns up and the volume tools
            // control.
            AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(OUTPUT_RATE)
                        .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                        .build(),
                )
                .setTransferMode(AudioTrack.MODE_STREAM)
                .setBufferSizeInBytes(max(minBuffer, OUTPUT_RATE * 2))
                .build()
        } catch (e: Exception) {
            onError("speaker unavailable: ${e.message}")
            return
        }

        if (player.state != AudioTrack.STATE_INITIALIZED) {
            player.release()
            onError("speaker unavailable")
            return
        }

        try {
            player.play()
        } catch (e: Exception) {
            player.release()
            onError("speaker unavailable: ${e.message}")
            return
        }
        track = player
        playRunning = true

        playThread = Thread({
            while (playRunning) {
                val item = try {
                    queue.poll(250, TimeUnit.MILLISECONDS)
                } catch (_: InterruptedException) {
                    break
                } ?: continue

                when (item) {
                    is Item.Audio -> {
                        var offset = 0
                        while (offset < item.bytes.size && playRunning) {
                            val written = player.write(item.bytes, offset, item.bytes.size - offset)
                            if (written <= 0) {
                                android.util.Log.w("AudioEngine", "write returned $written")
                                break
                            }
                            offset += written
                        }
                        framesWritten += item.bytes.size / 2
                    }

                    Item.End -> {
                        val target = framesWritten
                        // Wait for the DAC to play out the tail. Must never hang
                        // forever: a stuck head position would kill this thread
                        // and silence every later reply.
                        val started = SystemClock.elapsedRealtime()
                        val remainingSec = (target - player.playbackHeadPosition.toLong())
                            .coerceAtLeast(0) / OUTPUT_RATE.toDouble()
                        val deadline = started + (remainingSec + 10) * 1000 + 5000
                        while (playRunning && player.playbackHeadPosition.toLong() < target) {
                            if (SystemClock.elapsedRealtime() > deadline) {
                                android.util.Log.w(
                                    "AudioEngine",
                                    "playback wait timeout target=$target head=${player.playbackHeadPosition}",
                                )
                                break
                            }
                            Thread.sleep(15)
                        }
                        android.util.Log.d(
                            "AudioEngine",
                            "playback done target=$target head=${player.playbackHeadPosition}",
                        )
                        onPlaybackDone()
                    }
                }
            }
        }, "playback").apply { start() }
    }

    fun micActive(): Boolean = micRunning

    fun enqueueAudio(bytes: ByteArray) {
        if (bytes.isNotEmpty()) queue.offer(Item.Audio(bytes))
    }

    fun enqueueEnd() {
        queue.offer(Item.End)
    }

    fun stop() {
        micRunning = false
        playRunning = false
        record?.stop()
        record?.release()
        record = null
        track?.stop()
        track?.release()
        track = null
        queue.clear()
    }

    companion object {
        const val INPUT_RATE = 16000
        const val OUTPUT_RATE = 22050
        const val CHUNK_BYTES = 2560 // 80 ms of 16 kHz mono PCM16
    }
}
