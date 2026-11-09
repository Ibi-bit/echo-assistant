package com.leonp.assistant

import android.app.AlarmManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.BatteryManager
import android.os.Build
import android.os.SystemClock
import android.provider.AlarmClock
import android.provider.Settings
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.max
import kotlin.math.min

/**
 * Executes model tools on the tablet. Everything here is safe to call from a
 * background thread: Rusty is reached over its local HTTP API and the clock app
 * is started with a NEW_TASK intent.
 */
class DeviceCommands(private val context: Context) {
    data class Result(val ok: Boolean, val message: String)

    private val prefs = context.getSharedPreferences("assistant_device", Context.MODE_PRIVATE)

    fun execute(action: String, args: JSONObject): Result {
        return try {
            when (action) {
                "set_alarm" -> setAlarm(
                    args.getInt("hour"),
                    args.getInt("minute"),
                    args.optString("label").trim(),
                )
                "set_timer" -> setTimer(args.getInt("seconds"), args.optString("label").trim())
                "cancel_alarm" -> cancelAlarm(args.optBoolean("all"))
                "cancel_timer" -> cancelTimer(args.optBoolean("all"))
                "set_volume" -> setVolume(args.getInt("level"))
                "change_volume" -> changeVolume(
                    args.optString("direction"),
                    args.optInt("steps", 3),
                )
                "get_volume" -> Result(true, "Media volume is ${volumePercent()} percent.")
                "media_control" -> mediaControl(args.optString("action"))
                else -> Result(false, "Unknown device command.")
            }
        } catch (e: Exception) {
            Result(false, "That command failed: ${e.message ?: e.javaClass.simpleName}")
        }
    }

    // --------------------------------------------------------------- clock app

    private fun setAlarm(hour: Int, minute: Int, label: String): Result {
        if (hour !in 0..23 || minute !in 0..59) return Result(false, "That is not a valid time.")
        val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
            putExtra(AlarmClock.EXTRA_HOUR, hour)
            putExtra(AlarmClock.EXTRA_MINUTES, minute)
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            if (label.isNotEmpty()) putExtra(AlarmClock.EXTRA_MESSAGE, label)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            context.startActivity(intent)
            Result(true, "Alarm set for ${clockTime(hour, minute)}.")
        } catch (e: Exception) {
            Result(false, "I couldn't open the clock app: ${e.message}")
        }
    }

    private fun setTimer(seconds: Int, label: String): Result {
        if (seconds <= 0) return Result(false, "The timer needs a positive length.")
        val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
            putExtra(AlarmClock.EXTRA_LENGTH, seconds)
            putExtra(AlarmClock.EXTRA_SKIP_UI, true)
            if (label.isNotEmpty()) putExtra(AlarmClock.EXTRA_MESSAGE, label)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            context.startActivity(intent)
            val end = System.currentTimeMillis() + seconds * 1000L
            saveTimer(label.ifEmpty { "Timer" }, end)
            Result(true, "Timer set for ${duration(seconds)}.")
        } catch (e: Exception) {
            Result(false, "I couldn't open the clock app: ${e.message}")
        }
    }

    private fun cancelAlarm(all: Boolean): Result {
        val intent = Intent(AlarmClock.ACTION_DISMISS_ALARM).apply {
            putExtra(
                AlarmClock.EXTRA_ALARM_SEARCH_MODE,
                if (all) AlarmClock.ALARM_SEARCH_MODE_ALL else AlarmClock.ALARM_SEARCH_MODE_NEXT,
            )
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            context.startActivity(intent)
            Result(true, if (all) "All alarms dismissed." else "The next alarm is dismissed.")
        } catch (e: Exception) {
            Result(false, "I couldn't dismiss the alarm: ${e.message}")
        }
    }

    private fun cancelTimer(all: Boolean): Result {
        val intent = Intent(AlarmClock.ACTION_DISMISS_TIMER).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        return try {
            context.startActivity(intent)
            if (all) clearTimers() else removeNextTimer()
            Result(true, "Timer dismissed.")
        } catch (e: Exception) {
            Result(false, "I couldn't dismiss the timer: ${e.message}")
        }
    }

    // ---------------------------------------------------------------- volume

    private fun audio(): AudioManager = context.getSystemService(AudioManager::class.java)

    private fun volumePercent(): Int {
        val manager = audio()
        val max = manager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        if (max <= 0) return 0
        return manager.getStreamVolume(AudioManager.STREAM_MUSIC) * 100 / max
    }

    private fun setVolume(level: Int): Result {
        val value = level.coerceIn(0, 100)
        val manager = audio()
        val max = manager.getStreamMaxVolume(AudioManager.STREAM_MUSIC)
        manager.setStreamVolume(AudioManager.STREAM_MUSIC, max * value / 100, 0)
        return Result(true, "Media volume is now $value percent.")
    }

    private fun changeVolume(direction: String, steps: Int): Result {
        val manager = audio()
        val count = steps.coerceIn(1, 10)
        when (direction.lowercase(Locale.US)) {
            "up" -> repeat(count) {
                manager.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_RAISE, 0)
            }
            "down" -> repeat(count) {
                manager.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_LOWER, 0)
            }
            "mute" -> manager.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_MUTE, 0)
            "unmute" -> manager.adjustStreamVolume(AudioManager.STREAM_MUSIC, AudioManager.ADJUST_UNMUTE, 0)
            else -> return Result(false, "I can turn the volume up, down, mute, or unmute.")
        }
        val muted = manager.isStreamMute(AudioManager.STREAM_MUSIC)
        return Result(
            true,
            if (muted) "Media volume is muted."
            else "Media volume is now ${volumePercent()} percent.",
        )
    }

    // ------------------------------------------------------------ Rusty/media

    private fun mediaControl(action: String): Result {
        val state = rustyState() ?: return Result(false, "Rusty isn't responding on this device.")
        val playing = state.optJSONObject("playing")?.optBoolean("spotify") == true
        val nowPlaying = NowPlayingService.nowPlaying()
        val title = nowPlaying?.first

        if (action == "restart") {
            val result = rustyPost("/api/seek", JSONObject().put("positionMs", 0))
            return if (result != null) Result(true, "Restarted the current track.")
            else Result(false, "There is no Spotify track to restart.")
        }

        if (!playing && title == null) {
            return Result(false, "Spotify isn't playing right now.")
        }
        if (!NowPlayingService.isEnabled(context)) {
            return Result(false, "Enable now-playing access in the assistant setup first.")
        }

        val app = state.optJSONObject("app")
        if (app?.optBoolean("foreground") != true) {
            rustyPost("/api/foreground", JSONObject().put("on", true))
            Thread.sleep(180)
        }
        val panel = state.optJSONObject("panel")?.optString("active")
        if (panel != "spotify") {
            rustyPost("/api/panel", JSONObject().put("id", "spotify"))
            Thread.sleep(300)
        }

        if (!NowPlayingService.performMediaAction(action, 1500)) {
            return Result(false, "I couldn't press the Spotify control.")
        }
        return Result(
            true,
            when (action) {
                "play" -> "Playing."
                "pause" -> "Paused."
                "next" -> "Skipped to the next track."
                "previous" -> "Went back a track."
                else -> "Toggled playback."
            },
        )
    }

    private fun rustyState(): JSONObject? = rustyRequest("GET", "/api/state", null)

    private fun rustyPost(path: String, body: JSONObject): JSONObject? =
        rustyRequest("POST", path, body)

    private fun rustyRequest(method: String, path: String, body: JSONObject?): JSONObject? {
        val connection = try {
            (URL("http://127.0.0.1:8765$path").openConnection() as HttpURLConnection).apply {
                requestMethod = method
                connectTimeout = 800
                readTimeout = 800
                setRequestProperty("Accept", "application/json")
                if (body != null) {
                    doOutput = true
                    setRequestProperty("Content-Type", "application/json")
                }
            }
        } catch (_: Exception) {
            return null
        }
        return try {
            if (body != null) {
                connection.outputStream.use { it.write(body.toString().toByteArray()) }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader()?.use { it.readText() }.orEmpty()
            if (code in 200..299 && text.isNotBlank()) JSONObject(text) else null
        } catch (_: Exception) {
            null
        } finally {
            connection.disconnect()
        }
    }

    // ----------------------------------------------------------- device context

    fun buildContext(): String {
        val builder = StringBuilder()

        val battery = batteryLine()
        if (battery.isNotEmpty()) builder.append(battery).append('\n')
        builder.append(
            "Device: ${Build.MANUFACTURER} ${Build.MODEL}, Android ${Build.VERSION.RELEASE} " +
                "(SDK ${Build.VERSION.SDK_INT}), uptime ${duration((SystemClock.elapsedRealtime() / 1000).toInt())}.\n",
        )

        val manager = audio()
        val media = volumePercent()
        val muted = manager.isStreamMute(AudioManager.STREAM_MUSIC)
        val alarmMax = manager.getStreamMaxVolume(AudioManager.STREAM_ALARM)
        val alarm = if (alarmMax > 0) manager.getStreamVolume(AudioManager.STREAM_ALARM) * 100 / alarmMax else 0
        builder.append("Audio: media $media%${if (muted) " (muted)" else ""}, alarm $alarm%.\n")

        val network = networkLine()
        if (network.isNotEmpty()) builder.append(network).append('\n')

        val state = rustyState()
        if (state == null) {
            builder.append("Rusty: not responding on this device.\n")
        } else {
            val screen = state.optJSONObject("screen")
            val app = state.optJSONObject("app")
            val panel = state.optJSONObject("panel")?.optString("active").orEmpty()
            val playing = state.optJSONObject("playing")
            val spotify = playing?.optBoolean("spotify") == true
            val dlna = playing?.optBoolean("dlna") == true
            builder.append(
                "Rusty: ${if (app?.optBoolean("foreground") == true) "foreground" else "not foreground"}, " +
                    "panel ${panel.ifEmpty { "unknown" }}, screen ${if (screen?.optBoolean("on") == true) "on" else "off"}, " +
                    "brightness ${screen?.optInt("brightness", -1) ?: -1}%.\n",
            )
            val nowPlaying = NowPlayingService.nowPlaying()
            when {
                spotify -> {
                    val elapsed = formatMillis(playing?.optLong("elapsedMs", 0L) ?: 0L)
                    val total = formatMillis(playing?.optLong("durationMs", 0L) ?: 0L)
                    if (nowPlaying != null) {
                        builder.append(
                            "Spotify: playing \"${nowPlaying.first}\" by ${nowPlaying.second} " +
                                "at $elapsed of $total.\n",
                        )
                    } else {
                        builder.append("Spotify: playing (track title unavailable).\n")
                    }
                }
                nowPlaying != null -> builder.append(
                    "Spotify: paused on \"${nowPlaying.first}\" by ${nowPlaying.second}.\n",
                )
                dlna -> builder.append("DLNA: playing.\n")
                else -> builder.append("Spotify: not playing.\n")
            }
        }

        nextAlarmLine()?.let { builder.append(it).append('\n') }
        timersLine()?.let { builder.append(it).append('\n') }
        return builder.toString().trim().ifEmpty { "No device context available." }
    }

    private fun batteryLine(): String {
        val intent = try {
            context.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
        } catch (_: Exception) {
            null
        } ?: return ""
        val level = intent.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent.getIntExtra(BatteryManager.EXTRA_SCALE, 100)
        if (level < 0 || scale <= 0) return ""
        val percent = level * 100 / scale
        val status = intent.getIntExtra(BatteryManager.EXTRA_STATUS, -1)
        val charging = status == BatteryManager.BATTERY_STATUS_CHARGING ||
            status == BatteryManager.BATTERY_STATUS_FULL
        val plugged = intent.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) != 0
        val state = when {
            charging -> "charging"
            plugged -> "plugged in"
            else -> "on battery"
        }
        return "Battery: $percent%, $state."
    }

    private fun networkLine(): String {
        return try {
            val manager = context.getSystemService(ConnectivityManager::class.java)
            val network = manager.activeNetwork ?: return ""
            val capabilities = manager.getNetworkCapabilities(network)
            val transport = when {
                capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true -> "Wi-Fi"
                capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) == true -> "Ethernet"
                capabilities?.hasTransport(NetworkCapabilities.TRANSPORT_CELLULAR) == true -> "cellular"
                else -> "network"
            }
            val ip = manager.getLinkProperties(network)?.linkAddresses
                ?.firstOrNull { it.address is Inet4Address }
                ?.address?.hostAddress
            if (ip == null) "Network: $transport connected." else "Network: $transport, IP $ip."
        } catch (_: Exception) {
            ""
        }
    }

    private fun nextAlarmLine(): String? {
        val alarm = context.getSystemService(AlarmManager::class.java).nextAlarmClock ?: return null
        val at = alarm.triggerTime
        if (at <= System.currentTimeMillis()) return null
        val formatter = SimpleDateFormat("h:mm a", Locale.US)
        val seconds = ((at - System.currentTimeMillis()) / 1000).toInt()
        return "Next alarm: ${formatter.format(Date(at))} (in ${duration(seconds)})."
    }

    private fun timersLine(): String? {
        val timers = loadTimers()
        if (timers.isEmpty()) return null
        val now = System.currentTimeMillis()
        val parts = timers.joinToString(", ") { (label, end) ->
            "$label ${duration(((end - now) / 1000).toInt().coerceAtLeast(0))} left"
        }
        return "Assistant-set timers: $parts."
    }

    private fun saveTimer(label: String, end: Long) {
        val timers = loadTimers().toMutableList()
        timers.add(label to end)
        val array = JSONArray()
        for ((name, at) in timers) {
            array.put(JSONObject().put("label", name).put("end", at))
        }
        prefs.edit().putString("timers", array.toString()).apply()
    }

    private fun removeNextTimer() {
        val timers = loadTimers().toMutableList()
        if (timers.isNotEmpty()) timers.removeAt(0)
        saveTimers(timers)
    }

    private fun clearTimers() = saveTimers(emptyList())

    private fun saveTimers(timers: List<Pair<String, Long>>) {
        val array = JSONArray()
        for ((name, at) in timers) {
            array.put(JSONObject().put("label", name).put("end", at))
        }
        prefs.edit().putString("timers", array.toString()).apply()
    }

    private fun loadTimers(): List<Pair<String, Long>> {
        val raw = prefs.getString("timers", "[]") ?: "[]"
        val now = System.currentTimeMillis()
        val result = mutableListOf<Pair<String, Long>>()
        try {
            val array = JSONArray(raw)
            for (i in 0 until array.length()) {
                val item = array.optJSONObject(i) ?: continue
                val end = item.optLong("end", 0L)
                if (end > now) result.add(item.optString("label", "Timer") to end)
            }
        } catch (_: Exception) {
            return emptyList()
        }
        if (result.size != JSONArray(raw).length()) saveTimers(result)
        return result
    }

    // ---------------------------------------------------------------- formatting

    private fun clockTime(hour: Int, minute: Int): String {
        val formatter = SimpleDateFormat("h:mm a", Locale.US)
        return formatter.format(Date().apply {
            setHours(hour)
            setMinutes(minute)
            setSeconds(0)
        })
    }

    private fun duration(seconds: Int): String {
        val safe = max(0, seconds)
        val hours = safe / 3600
        val minutes = (safe % 3600) / 60
        val secs = safe % 60
        return when {
            hours > 0 -> "${hours}h ${minutes}m"
            minutes > 0 -> "${minutes}m ${secs}s"
            else -> "${secs}s"
        }
    }

    private fun formatMillis(millis: Long): String {
        val total = (millis / 1000).coerceAtLeast(0)
        return "${total / 60}:${(total % 60).toString().padStart(2, '0')}"
    }
}
