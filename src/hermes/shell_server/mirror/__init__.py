"""Mirror nativo — espejo + control de la sesión Hermes Shell por WebSocket.

Captura vía mutter ScreenCast (proven) + inyección de input vía mutter
RemoteDesktop, servido por WebSocket con auth por token. Sin GRD, sin llavero,
sin DES de VNC. Lo expone cloudflared como URL pública.
"""
