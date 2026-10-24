export const BUSINESS_TYPES = [
  { value: "fashion_apparel", label: "Fashion & Apparel" },
  { value: "beauty_skincare", label: "Beauty & Skincare" },
  { value: "health_wellness", label: "Health & Wellness" },
  { value: "home_living", label: "Home & Living" },
  { value: "electronics", label: "Electronics" },
  { value: "food_beverage", label: "Food & Beverage" },
  { value: "other", label: "Other" },
] as const;

export function businessTypeLabel(value: string | null): string | null {
  if (!value) return null;
  return BUSINESS_TYPES.find((t) => t.value === value)?.label ?? value;
}
