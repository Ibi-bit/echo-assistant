package com.leonp.assistant

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import org.json.JSONObject
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.ScheduledExecutorService
import java.util.concurrent.TimeUnit

/**
 * Keeps the microphone, relay connection and speaker alive while Rusty owns the screen.
 * The only thing this service draws is a small [OverlayController] card.
 */
class AssistantService : Service(), RelayClient.Listener {
    companion object {
        private const val CHANNEL = "assistant"
        private const val NOTIFICATION_ID = 41
        const val EXTRA_URL = "relay_url"
        const val EXTRA_QUERY = "query_text"

        @Volatile
        var active = false
            private set

        fun start(context: Context, url: String? = null) {
            val intent = Intent(context, AssistantService::class.java)
            if (url != null) intent.putExtra(EXTRA_URL, url)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, AssistantService::class.java))
        }

        /** Send a typed request to the relay without touching the mic/wake word. */
        fun injectQuery(context: Context, text: String) {
            val intent = Intent(context, AssistantService::class.java)
            intent.putExtra(EXTRA_URL, "") // keep URL unchanged
            intent.putExtra(EXTRA_QUERY, text.trim())
            context.startForegroundService(intent)
        }
    }

    private lateinit var settings: Settings
    private lateinit var overlay: OverlayController
    private lateinit var commands: DeviceCommands
    private val main = Handler(Looper.getMainLooper())
    private val replyText = StringBuilder()

    private var relay: RelayClient? = null
    private var audio: AudioEngine? = null
    private var commandExecutor: ExecutorService? = null
    private var contextExecutor: ScheduledExecutorService? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var focusRequest: AudioFocusRequest? = null
    private var reconnectScheduled = false

    @Volatile
    private var running = false

    private var speaking = false

    // Stop uploading microphone audio while the assistant is thinking or
    // talking: the server discards it anyway, and a burst of queued chunks
    // would otherwise eat the next listening window.
    @Volatile
    private var micGate = true
    private var speechClockStarted = false
    private var speechFrames = 0L

    private val audioRetry = object : Runnable {
        override fun run() {
            val engine = audio ?: return
            if (!running) return
            engine.start()
            if (!engine.micActive()) main.postDelayed(this, 3000)
        }
    }

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        createChannel()
        startForeground(NOTIFICATION_ID, notification("Starting…"))
        overlay = OverlayController(
            this,
            onTap = {
                val client = relay
                if (client == null) connect() else client.sendJson("""{"type":"listen"}""")
            },
            onLongPress = {
                startActivity(
                    Intent(this, MainActivity::class.java)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                )
            },
        )
        if (android.provider.Settings.canDrawOverlays(this)) overlay.show()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val url = intent?.getStringExtra(EXTRA_URL)?.trim().orEmpty()
        if (url.isNotEmpty()) {
            settings.relayUrl = url
            if (running) connect()
        }
        ensureStarted()
        val query = intent?.getStringExtra(EXTRA_QUERY)?.trim().orEmpty()
        if (query.isNotEmpty()) {
            // Typed test input. Safe to drop if the relay isn't up yet.
            val payload = JSONObject().put("type", "text").put("text", query).toString()
            relay?.sendJson(payload)
        }
        return START_STICKY
    }

    private fun ensureStarted() {
        if (running) return
        running = true
        active = true
        updateNotification("Listening for “Hey Jarvis”")
        commands = DeviceCommands(this)
        commandExecutor = Executors.newCachedThreadPool()
        contextExecutor = Executors.newSingleThreadScheduledExecutor().also { executor ->
            executor.scheduleWithFixedDelay({
                if (!running) return@scheduleWithFixedDelay
                val text = try {
                    commands.buildContext()
                } catch (e: Exception) {
                    "Device context unavailable: ${e.message}"
                }
                relay?.sendJson(
                    JSONObject()
                        .put("type", "device_context")
                        .put("text", text)
                        .toString(),
                )
            }, 1, 5, TimeUnit.SECONDS)
        }

        wakeLock = (getSystemService(POWER_SERVICE) as PowerManager)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "assistant:service")
            .apply {
                setReferenceCounted(false)
                acquire()
            }

        audio = AudioEngine(
            onChunk = { chunk ->
                if (micGate) relay?.sendAudio(chunk)
                main.post { overlay.setLevel(rmsNorm(chunk)) }
            },
            onPlaybackDone = {
                relay?.sendJson("""{"type":"playback_done"}""")
                main.post { abandonFocus() }
            },
            onError = { message ->
                main.post {
                    overlay.setState("Audio error")
                    overlay.setDetail(message)
                }
            },
        ).also {
            it.start()
            // Android audio is sometimes not ready at BOOT_COMPLETED.
            if (!it.micActive()) main.postDelayed(audioRetry, 3000)
        }

        if (android.provider.Settings.canDrawOverlays(this)) {
            overlay.show()
        } else {
            updateNotification("Allow display over other apps")
        }
        connect()
    }

    private fun connect() {
        reconnectScheduled = false
        relay?.close()
        replyText.setLength(0)
        overlay.setState("Connecting…")
        overlay.clearDetail()
        relay = RelayClient(settings.relayUrl, this).also { it.connect() }
    }

    override fun onOpen(client: RelayClient) {
        if (client !== relay) return
        main.post {
            overlay.setIcon("idle")
            overlay.setState("Hey Jarvis")
            overlay.clearDetail()
            updateNotification("Listening for “Hey Jarvis”")
        }
        client.sendJson("""{"type":"hello","version":1}""")
    }

    override fun onEvent(client: RelayClient, event: JSONObject) {
        if (client !== relay) return
        val kind = event.optString("type")
        if (kind == "audio_end") {
            if (speechFrames > 0) {
                overlay.setSpeechDuration(speechFrames / AudioEngine.OUTPUT_RATE.toFloat())
            }
            audio?.enqueueEnd()
            return
        }
        main.post { handleEvent(kind, event) }
    }

    private fun handleEvent(kind: String, event: JSONObject) {
        when (kind) {
            "command" -> {
                val id = event.optString("id")
                // Server sends the tool name under "name" (not "action") so the
                // inner "action" arg of media_control can't overwrite it.
                val action = event.optString("name").ifEmpty { event.optString("action") }
                val args = JSONObject(event.toString())
                android.util.Log.d("AssistantService", "command $action id=$id")
                val executor = commandExecutor
                if (executor == null) {
                    sendCommandResult(id, false, "The assistant is not ready.")
                    return
                }
                executor.execute {
                    try {
                        val result = commands.execute(action, args)
                        android.util.Log.d("AssistantService", "command $action done ok=${result.ok} msg=${result.message}")
                        sendCommandResult(id, result.ok, result.message)
                    } catch (e: Exception) {
                        android.util.Log.e("AssistantService", "command $action crashed", e)
                        sendCommandResult(id, false, "Command crashed: ${e.message}")
                    }
                }
            }

            "ready" -> {
                overlay.setIcon("idle")
                overlay.setState("Hey Jarvis")
                overlay.clearDetail()
            }

            "state" -> when (event.optString("value")) {
                "idle" -> {
                    micGate = true
                    speaking = false
                    overlay.endSpeech()
                    overlay.setIcon("idle")
                    overlay.setState("Hey Jarvis")
                    overlay.clearDetailSoon()
                    abandonFocus()
                }

                "listening" -> {
                    micGate = true
                    overlay.setIcon("listening")
                    overlay.setState("Listening…")
                    overlay.clearDetail()
                }

                "thinking" -> {
                    micGate = false
                    overlay.setIcon("thinking")
                    overlay.setState("Thinking…")
                }

                "speaking" -> {
                    micGate = false
                    speaking = true
                    speechClockStarted = false
                    speechFrames = 0L
                    overlay.setIcon("speaking")
                    overlay.setState("Speaking…")
                }
            }

            "wake" -> {
                replyText.setLength(0)
                overlay.clearDetail()
            }

            "transcript" -> {
                overlay.setDetail("“${event.optString("text")}”")
            }

            "delta" -> {
                replyText.append(event.optString("text"))
                overlay.setDetail(replyText.toString())
            }

            "reply" -> {
                replyText.setLength(0)
                replyText.append(event.optString("text"))
                overlay.setDetail(replyText.toString())
            }

            "notice" -> overlay.setDetail(event.optString("text"))

            "error" -> {
                overlay.setIcon("error")
                overlay.setState("Error")
                overlay.setDetail(event.optString("message"))
            }

            "reset" -> {
                replyText.setLength(0)
                overlay.clearDetail()
                overlay.setState("Hey Jarvis")
            }
        }
    }

    private fun sendCommandResult(id: String, ok: Boolean, message: String) {
        relay?.sendJson(
            JSONObject()
                .put("type", "command_result")
                .put("id", id)
                .put("ok", ok)
                .put("message", message)
                .toString(),
        )
    }

    override fun onAudio(client: RelayClient, pcm: ByteArray) {
        if (client !== relay) return
        if (focusRequest == null) main.post { requestFocus() }
        speechFrames += (pcm.size / 2).toLong()
        if (speaking && !speechClockStarted) {
            speechClockStarted = true
            overlay.beginSpeech()
        }
        audio?.enqueueAudio(pcm)
    }

    override fun onClosed(client: RelayClient, reason: String, willRetry: Boolean) {
        if (client !== relay) return
        main.post {
            overlay.setIcon("idle")
            overlay.setState("Offline — retrying…")
        }
        if (!willRetry || !running || reconnectScheduled) return
        reconnectScheduled = true
        main.postDelayed({
            if (running) connect()
        }, 3000)
    }

    /** Normalized mic RMS 0..1 for the listening EQ icon. */
    private fun rmsNorm(chunk: ByteArray): Float {
        if (chunk.size < 2) return 0f
        var sum = 0L
        var count = 0
        var i = 0
        while (i + 1 < chunk.size) {
            val sample = ((chunk[i + 1].toInt() shl 8) or (chunk[i].toInt() and 0xFF)).toShort()
            sum += sample.toLong() * sample.toLong()
            count++
            i += 2
        }
        if (count == 0) return 0f
        val rms = kotlin.math.sqrt(sum.toDouble() / count) / 32768.0
        return (rms * 4.0).toFloat().coerceIn(0f, 1f)
    }

    private fun requestFocus() {
        if (focusRequest != null) return
        val manager = getSystemService(AudioManager::class.java)
        val attributes = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
            .build()
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT)
            .setAudioAttributes(attributes)
            .setOnAudioFocusChangeListener({ }, main)
            .build()
        // Keep the request even if focus is refused: it stops us re-asking on every chunk,
        // and abandoning a refused request is harmless.
        focusRequest = request
        manager.requestAudioFocus(request)
    }

    private fun abandonFocus() {
        focusRequest?.let {
            getSystemService(AudioManager::class.java).abandonAudioFocusRequest(it)
        }
        focusRequest = null
    }

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, "Assistant", NotificationManager.IMPORTANCE_LOW)
                .apply {
                    description = "Keeps the assistant listening in the background"
                    setShowBadge(false)
                },
        )
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL)
            .setContentTitle("Assistant")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_assistant)
            .setOngoing(true)
            .setContentIntent(open)
            .build()
    }

    private fun updateNotification(text: String) {
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, notification(text))
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        running = false
        active = false
        main.removeCallbacksAndMessages(null)
        contextExecutor?.shutdownNow()
        contextExecutor = null
        commandExecutor?.shutdownNow()
        commandExecutor = null
        relay?.close()
        relay = null
        audio?.stop()
        audio = null
        overlay.remove()
        wakeLock?.release()
        wakeLock = null
        abandonFocus()
        super.onDestroy()
    }
}
