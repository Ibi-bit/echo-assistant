package com.leonp.assistant

import android.Manifest
import android.app.Activity
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import org.json.JSONObject

/**
 * Setup and permissions only. It is a dialog so Rusty stays visible behind it, and it
 * finishes as soon as the service starts. Runtime visuals live in [OverlayController].
 */
class MainActivity : Activity() {
    private lateinit var settings: Settings
    private lateinit var status: TextView
    private lateinit var address: EditText
    private lateinit var overlayButton: Button
    private lateinit var micButton: Button
    private lateinit var accessButton: Button
    private lateinit var accessStatus: TextView

    private var pendingStart = false
    private var micPrompted = false
    private var overlayPrompted = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        settings = Settings(this)
        status = findViewById(R.id.status)
        address = findViewById(R.id.relay_url)
        overlayButton = findViewById(R.id.overlay_button)
        micButton = findViewById(R.id.mic_button)
        accessButton = findViewById(R.id.access_button)
        accessStatus = findViewById(R.id.access_status)
        address.setText(settings.relayUrl)

        val requested = if (BuildConfig.DEBUG) {
            intent?.getStringExtra("relay_url")?.trim().orEmpty()
        } else {
            ""
        }
        if (requested.isNotEmpty()) {
            settings.relayUrl = requested
            address.setText(requested)
            pendingStart = true
        }
        if (BuildConfig.DEBUG && intent?.getBooleanExtra("start", false) == true) {
            pendingStart = true
        }

        overlayButton.setOnClickListener {
            overlayPrompted = false
            openOverlaySettings()
        }
        micButton.setOnClickListener {
            micPrompted = false
            requestMic()
        }
        accessButton.setOnClickListener { openAccessibilitySettings() }
        val testInput = findViewById<EditText>(R.id.test_input)
        findViewById<Button>(R.id.send_text_button).setOnClickListener {
            val text = testInput.text.toString().trim()
            if (text.isEmpty()) return@setOnClickListener
            if (!AssistantService.active) {
                status.text = "Start the assistant first, then send."
                return@setOnClickListener
            }
            AssistantService.injectQuery(this, text)
            // Keep the dialog open (Rusty stays behind it) so tests can be chained.
            status.text = "Sent: $text"
        }
        findViewById<Button>(R.id.start_button).setOnClickListener {
            settings.relayUrl = address.text.toString().trim()
            micPrompted = false
            overlayPrompted = false
            pendingStart = true
            startIfReady()
        }
        findViewById<Button>(R.id.stop_button).setOnClickListener {
            AssistantService.stop(this)
            status.text = "Assistant stopped"
        }

        maybeRunTest(intent)
        refresh()
    }

    override fun onResume() {
        super.onResume()
        refresh()
        if (pendingStart) startIfReady()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        maybeRunTest(intent)
        val requested = if (BuildConfig.DEBUG) {
            intent.getStringExtra("relay_url")?.trim().orEmpty()
        } else {
            ""
        }
        if (requested.isNotEmpty()) {
            settings.relayUrl = requested
            address.setText(requested)
            pendingStart = true
            startIfReady()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) {
            micPrompted = false
        }
        refresh()
        if (pendingStart) startIfReady()
    }

    private fun refresh() {
        val mic = hasMic()
        val overlay = android.provider.Settings.canDrawOverlays(this)

        micButton.text = if (mic) "Microphone allowed" else "Allow microphone"
        micButton.isEnabled = !mic
        overlayButton.text =
            if (overlay) "Overlay allowed" else "Allow display over other apps"
        overlayButton.isEnabled = !overlay

        val access = NowPlayingService.isEnabled(this)
        accessButton.text =
            if (access) "Now-playing access on" else "Allow now-playing access"
        accessButton.isEnabled = !access
        accessStatus.text = if (access) {
            "On: the assistant can read the track Rusty shows and press play, pause, next, and previous."
        } else {
            "Optional. Lets the assistant see the track Rusty is showing and press its playback buttons."
        }

        status.text = when {
            !mic && !overlay -> "Grant microphone and overlay access."
            !mic -> "Grant microphone access."
            !overlay -> "Allow display over other apps; Rusty stays visible underneath."
            AssistantService.active -> "Assistant is running over Rusty."
            else -> "Ready. Rusty keeps the screen; the assistant floats above it."
        }
    }

    private fun startIfReady() {
        if (!hasMic()) {
            if (!micPrompted) {
                micPrompted = true
                requestMic()
            }
            return
        }
        if (!android.provider.Settings.canDrawOverlays(this)) {
            if (!overlayPrompted) {
                overlayPrompted = true
                openOverlaySettings()
            }
            return
        }
        settings.relayUrl = address.text.toString().trim()
        AssistantService.start(this, settings.relayUrl)
        pendingStart = false
        finish()
    }

    private fun requestMic() {
        if (!hasMic()) {
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), 1)
        }
    }

    private fun hasMic(): Boolean =
        checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    // ------------------------------------------------------------ test hook
    //
    // adb shell 'am start -n com.leonp.assistant/.MainActivity \
    //   --es test_name set_timer --es test_args "{\"seconds\":60}"'
    // Runs the real DeviceCommands code path on-device and logs the result
    // under tag "AssistantTest". test_name=get_device_status logs the live
    // device-context string instead. This is how the tools are verified
    // without relying on wake-word acoustics through the tiny speaker.

    private fun maybeRunTest(intent: Intent?) {
        if (!BuildConfig.DEBUG) return
        // Typed request injection for testing without wake word / mic.
        val query = intent?.getStringExtra("test_text")?.trim().orEmpty()
        if (query.isNotEmpty()) {
            AssistantService.injectQuery(this, query)
            status.text = "Sent: $query"
            return
        }
        val name = intent?.getStringExtra("test_name")?.trim().orEmpty()
        if (name.isEmpty()) return
        val raw = intent?.getStringExtra("test_args").orEmpty().ifEmpty { "{}" }
        Thread {
            val result = try {
                if (name == "get_device_status") {
                    "context: " + DeviceCommands(this).buildContext()
                } else {
                    val args = try {
                        JSONObject(raw)
                    } catch (_: Exception) {
                        JSONObject()
                    }
                    val r = DeviceCommands(this).execute(name, args)
                    "ok=${r.ok} msg=${r.message}"
                }
            } catch (e: Exception) {
                "crashed: ${e.message}"
            }
            android.util.Log.d("AssistantTest", "test $name $raw -> $result")
            runOnUiThread { status.text = "$name: $result" }
        }.start()
    }

    private fun openOverlaySettings() {
        if (android.provider.Settings.canDrawOverlays(this)) return
        startActivity(
            Intent(
                android.provider.Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                Uri.parse("package:$packageName"),
            ),
        )
    }

    private fun openAccessibilitySettings() {
        val component = ComponentName(this, NowPlayingService::class.java)
        val specific = Intent("android.settings.ACCESSIBILITY_DETAILS_SETTINGS").apply {
            putExtra(Intent.EXTRA_COMPONENT_NAME, component.flattenToString())
        }
        try {
            startActivity(specific)
        } catch (_: Exception) {
            startActivity(Intent(android.provider.Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }
    }
}
