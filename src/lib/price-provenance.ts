import { z } from "zod";

const datedCharge = z.object({
	effective_date: z.iso.date(),
	header: z.string().min(1),
	column_1based: z.number().int().positive(),
});

/** A retrieval timestamp is not a price's effective date. */
export function grossChargeDate(value: unknown): string | null {
	if (value == null) return null;
	const parsed = datedCharge.safeParse(value);
	return parsed.success ? parsed.data.effective_date : null;
}
