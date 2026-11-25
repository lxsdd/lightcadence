# Chroma V2 transport candidate

Protocol reference: Yeelight/Yeelight-Chroma-Connector, commit
769a616b9012877baf7fa3df426b38ca6697a167, public
ChromaBroadcastSampleApplication/CUdpLight.cpp and CUdpLight.h.
The original sample declares BSD-3-Clause terms. No vendor source copied;
this independent adapter uses protocol identifiers, UDP port 55444, and the
documented example request shapes. The separate PDF was not accessible for
independent verification in this increment.

The adapter uses a connected IPv4 UDP socket with explicit acquire and renewal,
correlated IDs, candidate hex token validation and expiry. No discovery,
automatic reconnect, retransmission, V1 fallback, or asynchronous queued sends.
A connected socket filters by endpoint; it does not authenticate the device
or guarantee delivery. A token reply must match the request ID; that stricter
assumption, the keepalive reply shape, the conservative ten-second local
validity window and the linear-light to RGB/brightness mapping require real
model/firmware verification. Zero brightness maps to black at minimum
brightness, not confirmed power-off. The address validation conservatively
rejects final octets 0/255 because network masks are not yet modeled.

Software qualification: real UDP loopback sockets, a synthetic peer, original
coordinator and session manager. No external network or real lamp. No
positive physical output, confirmed restore, native HA handover, device profile enrollment, or device identity qualification.
The preview runtime is unchanged and cannot open this transport. Stop/restore
returns unconfirmed without a speculative write and retains the coordinator's
lease. Real stop/restore and end-to-end activation remain to be implemented
and owner-authorized before physical use.

The opt-in output profile now schedules renewal through the existing per-device
worker. It uses request-send time for expiry and rejects renewal after the
previous validity window. During a handshake only the latest mailbox is
consumed after a fresh ownership/generation/handover check. Failure closes the
transport without automatic acquisition. Stop and manual detach wait for the
in-flight handshake and prevent subsequent color frames. This is loopback-only
software behavior; no physical activation, identity or restore claim.
