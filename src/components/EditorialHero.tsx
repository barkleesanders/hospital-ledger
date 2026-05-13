/**
 * Editorial hero — eyebrow, massive italic serif title, lede paragraph.
 * Used on the procedure page (and reusable for any future article-style
 * landing page). The lede is intentionally `children` so callers can emit
 * inline-styled spans (e.g. amber/rose price ranges) without having to
 * pre-render HTML strings.
 */

import type { FC, PropsWithChildren } from "hono/jsx";

export type EditorialHeroProps = PropsWithChildren<{
  eyebrow: string;
  title: string;
}>;

export const EditorialHero: FC<EditorialHeroProps> = ({ eyebrow, title, children }) => (
  <section class="pb-10 mb-8 editorial-rule" style="border-bottom-style: solid;">
    <div
      class="label-eyebrow mb-3"
      // eyebrow is allowed to contain trusted inline HTML (mono code spans, hospital count)
      // biome-ignore lint/security/noDangerouslySetInnerHtml: server-built trusted HTML
      dangerouslySetInnerHTML={{ __html: eyebrow }}
    />
    <h1 class="serif text-5xl md:text-7xl leading-[1.05] text-zinc-50">{title}</h1>
    {children ? (
      <p class="mt-6 max-w-3xl text-lg md:text-xl text-zinc-300 leading-relaxed">{children}</p>
    ) : null}
  </section>
);
