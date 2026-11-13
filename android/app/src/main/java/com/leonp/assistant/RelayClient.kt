package com.leonp.assistant

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import org.json.JSONObject
import java.net.URI
import java.net.URLDecoder
import java.util.concurrent.TimeUnit

class RelayClient(
    private val url: String,
    private val listener: Listener,
) {
    interface Listener {
        fun onOpen(client: RelayClient)
        fun onEvent(client: RelayClient, event: JSONObject)
        fun onAudio(client: RelayClient, pcm: ByteArray)
        fun onClosed(client: RelayClient, reason: String, willRetry: Boolean)
    }

    private val http = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    private var socket: WebSocket? = null

    @Volatile
    private var open = false

    fun connect() {
        val uri = try {
            URI(url)
        } catch (_: Exception) {
            listener.onClosed(this@RelayClient, "Invalid relay URL", false)
            return
        }
        if (uri.scheme == "ws" && !isPrivateHost(uri.host)) {
            listener.onClosed(
                this@RelayClient,
                "Cleartext ws:// is only allowed on the LAN; use wss:// for a public host",
                false,
            )
            return
        }
        val token = uri.rawQuery
            ?.split("&")
            ?.firstOrNull { it.startsWith("token=") }
            ?.removePrefix("token=")
            ?.let { URLDecoder.decode(it, "UTF-8") }
        // Keep the token out of the URL (and access logs); send it as a header.
        val endpoint = url
            .replace(Regex("([?&])token=[^&]*"), "$1")
            .replace("?&", "?")
            .trimEnd('?', '&')
        val request = Request.Builder().url(endpoint).apply {
            if (token != null) header("Authorization", "Bearer $token")
        }.build()
        socket = http.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                open = true
                listener.onOpen(this@RelayClient)
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val event = try {
                    JSONObject(text)
                } catch (_: Exception) {
                    return
                }
                listener.onEvent(this@RelayClient, event)
            }

            override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
                if (bytes.size > 1 && bytes[0] == 0x01.toByte()) {
                    listener.onAudio(this@RelayClient, bytes.substring(1).toByteArray())
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                open = false
                listener.onClosed(this@RelayClient, "closed $code $reason", code != 1000)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                open = false
                listener.onClosed(this@RelayClient, t.message ?: "connection failed", true)
            }
        })
    }

    fun close() {
        open = false
        socket?.close(1000, "bye")
        socket = null
    }

    fun sendAudio(pcm: ByteArray): Boolean {
        val ws = socket ?: return false
        if (!open) return false
        return ws.send(pcm.toByteString())
    }

    fun sendJson(json: String): Boolean = socket?.send(json) ?: false

    private fun isPrivateHost(host: String?): Boolean {
        if (host == null) return false
        val bare = host.removeSurrounding("[", "]").lowercase()
        if (bare == "localhost" || bare == "::1") return true
        val parts = bare.split(".")
        if (parts.size != 4) return false
        val octets = parts.map { it.toIntOrNull() ?: return false }
        if (octets.any { it !in 0..255 }) return false
        return octets[0] == 10 || octets[0] == 127 ||
            (octets[0] == 172 && octets[1] in 16..31) ||
            (octets[0] == 192 && octets[1] == 168) ||
            (octets[0] == 169 && octets[1] == 254)
    }
}
