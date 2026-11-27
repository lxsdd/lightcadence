# Explicit white-light Yeelight TCP candidate

An owner-local device profile may specify transport `yeelight_tcp`, interval at least one second,
and all three integers `white_kelvin`, `ct_min_kelvin`, `ct_max_kelvin`.
The configured bounds must be inside the published 1700–6500 K protocol range and must be checked
against the actual model before enrollment. All other profiles retain their original RGB behavior.
Admission requires directed identity/capabilities and a complete native CT-mode snapshot
with no active flow or music mode. Missing/unsupported states fail closed.

The same engine projects linear RGB intensity onto the white channel using relative luminance;
the configured fixed Kelvin value is used for nonzero output and zero maps to power off.
This is not color reproduction, calibrated brightness, dynamic CT synthesis, or hardware qualification.
The same normal TCP quotas, coalescing, lease, stop and recovery rules apply.
Original Kelvin, brightness and power are saved and independently verified; legacy RGB recovery records
remain readable. No flash is attempted to modify hidden attributes on an already-off lamp;
an unconfirmed restoration remains pending. Explicit manual CT state supports integer Kelvin and
percentage brightness; RGB commands to these profiles are rejected before manual takeover.
No implicit switching of transport or mode, no automatic reconnect, and no new permissions.
Reference: https://www.yeelight.com/download/Yeelight_Inter-Operation_Spec.pdf
