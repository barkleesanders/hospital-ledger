/**
 * Visible breadcrumb trail for detail/index pages. Renders the same crumb
 * list the page's BreadcrumbList JSON-LD is built from (src/lib/seo.ts), so
 * the visible trail and the schema can never disagree.
 */

import type { FC } from "hono/jsx";
import type { Crumb } from "../lib/seo";

export const Breadcrumbs: FC<{ items: Crumb[] }> = ({ items }) => (
	<nav aria-label="Breadcrumb" class="mb-6">
		<ol class="flex flex-wrap items-center gap-1.5 text-sm text-zinc-400">
			{items.map((c, i) => {
				const last = i === items.length - 1;
				return (
					<li class="flex items-center gap-1.5">
						{i > 0 ? (
							<span aria-hidden="true" class="text-zinc-400">
								/
							</span>
						) : null}
						{last ? (
							<span aria-current="page" class="text-zinc-200">
								{c.name}
							</span>
						) : (
							<a
								href={c.path}
								class="underline-offset-4 hover:text-zinc-200 hover:underline"
							>
								{c.name}
							</a>
						)}
					</li>
				);
			})}
		</ol>
	</nav>
);
