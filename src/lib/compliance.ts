import { z } from "zod";

export const ComplianceRatingSchema = z.object({
	grade: z
		.string()
		.transform((grade) => grade.toUpperCase())
		.pipe(z.enum(["A", "B", "C", "D", "F"])),
	// Ceiling: compute_compliance in scripts/slim_parsed.py sums weights to 100.
	score: z.number().finite().min(0).max(100),
});

export type Grade = z.infer<typeof ComplianceRatingSchema>["grade"];

/** A grade is measured only when both the letter and score are valid. */
export function complianceRating(value: unknown) {
	const parsed = ComplianceRatingSchema.safeParse(value);
	return parsed.success ? parsed.data : null;
}

/** Producer flags represent coverage thresholds, not whether any row exists. */
export function complianceElementStatus(value: unknown) {
	return value === true
		? "assessment threshold met"
		: value === false
			? "below assessment threshold"
			: "unknown";
}
