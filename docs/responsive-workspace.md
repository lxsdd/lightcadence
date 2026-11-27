# Responsive workspace contract

The shared Lit workspace styles are the baseline for all LightCadence views.
They follow the approved adaptive dashboard principle: use the actual HA
content container, including sidebar/header changes; fill available width and
resolved height, align wide columns, stack proportionally on narrow surfaces,
retain HA theme colors and clear textual state, and never hide controls behind
internal scroll panes. For short or content-heavy views, natural outer-page
vertical scrolling takes priority over forcing everything into a fixed height.
No maximum desktop width and no viewport-width breakpoint or fixed subtraction
of shell chrome. Native CSS container queries reflow as parent width changes;
percentage minimum height and grid/flex stretching consume a definite parent
height. A parent with intrinsic/auto height grows with content instead.

The current live panel shares the responsive foundation across preview and
physical candidate modes. New scene/floorplan/timeline views must use the
same design contract; their detailed editor interactions remain separate
product work. Browser regressions resize a bounded HA-like parent without
changing the desktop viewport, verify alignment, width/height use, narrow
stacking, long text and no internal scroll. Real HA shell and Companion app
acceptance remains separate. No private dashboard source copied.
