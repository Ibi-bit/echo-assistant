package com.leonp.assistant

import android.content.Context
import android.content.SharedPreferences

class Settings(context: Context) {
    private val prefs: SharedPreferences =
        context.getSharedPreferences("assistant", Context.MODE_PRIVATE)

    var relayUrl: String
        get() = prefs.getString("relay_url", DEFAULT_URL) ?: DEFAULT_URL
        set(value) {
            prefs.edit().putString("relay_url", value).apply()
        }

    companion object {
        const val DEFAULT_URL = "ws://192.168.1.100:8765/ws"
    }
}
