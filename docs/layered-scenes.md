# Layered scenes v1
The existing scene effect object accepts kind layered, with a nonempty ordered
flat layers array. Each layer has effect (one complete leaf preset), opacity
[0,1] and blend (over, add, max). Static RGB is a normal leaf effect, usable as
ambient base. Leaf brightness multiplies opacity as coverage for over; add
clips channels, max takes channel-wise maximum. RGB is linear light. Normalize
the mixed result once, then apply parent brightness. Dark moving effects reveal
the base layer. All layers share session time and spatial positions, and at most
one explicitly selected live audio source; presets never persist source IDs.
No separate transport, lease, scheduler or browser-side effect renderer.
Existing leaf presets remain valid. Stack editing and saving do not alter
running sessions. Timeline scheduling, per-layer target masks, live revisioned
mixing and optical performance qualification are separate future blocks.
