package com.leonp.assistant

import android.Manifest
import android.app.Activity
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.FrameLayout

/**
 * Invisible launch pad for BOOT_COMPLETED. Android 11 refuses microphone access to a
 * foreground service started straight from the background, so this activity starts the
 * service while it is briefly visible, then disappears.
 */
class BootActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(FrameLayout(this))
    }

    override fun onResume() {
        super.onResume()
        val allowed = checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (allowed && android.provider.Settings.canDrawOverlays(this)) {
            AssistantService.start(this)
        }
        finish()
    }
}
