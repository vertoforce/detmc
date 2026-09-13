package dev.detmc;

/**
 * Phase 7 diagnostic. Implemented on {@code LegacyRandomSource} by
 * {@code LegacyRandomSourceMixin}: how many {@code next(bits)} draws this instance has
 * made, and its current 48-bit state. Two runs whose entity has the same seed and the
 * same draw count have executed the same random-consuming path up to that point.
 */
public interface DetDrawCounter {
    long detmc$draws();

    long detmc$state();
}
