# Nano Banana prompt — isometric photo tile → pixel art

Prompt used to convert the stitched Earth Studio tiles (`tiles/*.png`,
`subtiles/*`) into pixel-art tiles. The image is the near-orthographic 2:1
dimetric aerial capture described in [CAPTURE.md](CAPTURE.md).

Feed **one tile at a time** with the prompt below. Keep the wording identical
across tiles — consistency of style is what makes the tiles stitch back
together without visible seams.

---

## Main prompt

> Redraw this aerial photograph as hand-crafted isometric pixel art, in the
> style of a 16-bit city-builder game (think *SimCity 2000* / *Theme Hospital*
> / classic Amiga isometric tilesets).
>
> Keep the geometry exactly as it is. This is a 2:1 dimetric, near-orthographic
> top-down view: every roofline, street, courtyard and building footprint must
> stay in precisely the same position, at the same scale and the same angle as
> in the source. Do not re-imagine the layout, do not add or remove buildings,
> do not rotate or re-light the scene. The result must overlay the original
> pixel-for-pixel.
>
> Style:
> - Crisp, chunky pixels on a consistent grid — roughly 4×4 source pixels per
>   art pixel. Hard edges, no anti-aliasing, no blur, no soft gradients.
> - A limited palette of about 32 colours. Flat colour fills with 2–3 shading
>   steps per surface, dithered transitions where a gradient is needed.
> - One consistent light direction for the whole image: sun from the upper
>   left, so left-facing roof planes are lit, right-facing planes are in shade,
>   and every building drops a short, hard-edged shadow to the lower right.
> - Clean 1-pixel darker outlines along building edges and roof ridges so
>   individual structures read clearly.
> - Roofs simplified into readable shapes: terracotta reds and slate greys for
>   the old town, flat greys and blues for modern blocks, visible pixel texture
>   for tiled roofs.
> - Streets as flat grey with a slightly darker edge; tram lines, crossings and
>   road markings suggested with single-pixel lines.
> - Trees and parks as clusters of 2–3 green tones with dithered edges; the
>   Danube as flat blue-teal bands with a subtle dithered ripple pattern.
> - Cars, street furniture and small details reduced to 2–4 pixel blobs — legible
>   as objects, never photographic.
>
> No text, no labels, no logos, no watermark, no UI, no vignette, no depth of
> field, no photographic noise or film grain. Fill the entire frame edge to
> edge — no borders, no padding, no drop shadow around the image itself.
>
> Output the full square frame at the same aspect ratio as the input.

---

## Tips

- **Seams:** if adjacent tiles drift in palette, pass the already-converted
  neighbour tile alongside the new one and append: *"Match the palette, pixel
  size and lighting of the second image exactly; this tile sits directly to its
  right and must line up seamlessly."*
- **Too painterly:** add *"Fewer colours. Larger, harder pixels. No smooth
  shading — dither instead."*
- **Geometry drift:** add *"Trace the source image exactly; treat it as a
  reference underlay you are painting over."*
- **Overlap tiles:** convert the overlapping crops (see `offsets.json`) rather
  than exact tile bounds, so the blend region has room to hide differences.
