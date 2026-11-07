# Hospital appointment presentation redesign

Updated deck: `hospital-appointment-management.pptx`. The validated redesign copy contains the same bytes. The original is preserved as `hospital-appointment-management-before-redesign.pptx`.

All 10 English slides, the five-minute speaker script and every branch of the editable complete workflow are retained. Package integrity, font and layout checks, slide count, round-trip import and the separate Office validation passed. Every slide was rendered through LibreOffice and inspected. The embedded video is H.264, 780 × 440, 30 fps, 15 seconds, without audio. Actual playback in Microsoft PowerPoint was not exercised because PowerPoint is not installed here.

Slide 7: click the diagram during the slide show to play the optional educational video. The same clip is available at `assets/booking-recovery.mp4`. In the Selection Pane, the movie is named “Optional recovery walkthrough (click to play)”. Move it aside or hide it to edit the native diagram underneath. Slides advance manually and use a short fade transition. The optional clip adds 15 seconds to the original five-minute talk.

Use the Claude frontend-design skill as design direction, the presentation and PPTX skills for editable authoring, and ImageGen for one editorial cover illustration.

Palette: white #FFFFFF, cool mist #F1F7F6, ink #173B45, conversational teal #087E83, coordinator violet #7563A5, validation amber #9B6722, booking blue #416DA3. The four agent colours encode responsibilities and remain consistent across the diagrams.

Typography: Arial, bold for headings and role names. Large, left-aligned headings and generous whitespace. Diagram labels remain smaller only on the complete workflow, which must retain every branch.

Layouts: cover with illustration on the right; three actors in open columns; coordinator above three specialists; different explanatory diagrams for each agent; complete native workflow; three outcome columns; implementation architecture at the end. Use panels only where they represent actual process nodes or a video surface.

Self-review: a generic repeated card grid would obscure the relationships between the agents, so the overview uses a hierarchy and the agent slides use different diagrams. White and cool mist directly address the rejected black background. Lucide replaces the unrelated emoji. The only generated image introduces the patient and scheduling professional; technical content uses editable diagrams.

Motion: short manual fade transitions. Optional silent 15-second educational video on slide 7, triggered by clicking the diagram, showing search, selection, reservation conflict and retry. Full workflow remains editable on slide 8. No background video or automatic slide advance.

Image generation used the built-in ImageGen tool. Asset: `assets/hospital-booking-illustration.png`. Exact prompt:

> Use case: illustration-story. Asset type: square cover illustration for an academic PowerPoint about AI-assisted hospital appointment booking. Create one refined editorial healthcare illustration with a very light cool white background (#F4F8F7), an adult patient and a friendly hospital scheduling professional consulting a minimal appointment calendar together, plus a small abstract flow of connected scheduling nodes that subtly conveys coordinated software agents. Mature graphic design, soft geometric volumes, subtle paper texture, clean precise contours, airy composition, restrained teal (#087E83), muted blue (#3D6FA1), sage, and a tiny apricot accent. No robots, no brains, no sci-fi holograms, no stethoscope closeups, no stock-photo look, no typography, no letters, no numerals, no logos, no watermark. Confident, warm, academically credible. Objects and figures comfortably contained with generous whitespace at all edges. A single visually unified scene, not a collage or a slide mockup.

Icon source: bundled Lucide icon assets, ISC licence, https://lucide.dev/license. Icons rasterized from the upstream path definitions; saved source SVG assets remain available beside the deck.
