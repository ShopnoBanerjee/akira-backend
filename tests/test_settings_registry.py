"""The settings registry: the thing standing between a typo and broken scoring."""

import pytest

from app.core.settings_registry import REGISTRY, SettingDef, validate_value


def get(key: str) -> SettingDef:
    return REGISTRY[key]


class TestRegistryShape:
    def test_scoring_weights_default_to_the_spec_split(self) -> None:
        """0.50 / 0.30 / 0.20 from spec section 4.3."""
        weights = [
            get("scoring.weight.run_score").default,
            get("scoring.weight.completion_rate").default,
            get("scoring.weight.on_time_rate").default,
        ]
        assert weights == [0.50, 0.30, 0.20]
        assert sum(weights) == pytest.approx(1.0)

    def test_bands_default_to_the_spec_thresholds(self) -> None:
        assert get("scoring.band.green").default == 90
        assert get("scoring.band.amber").default == 75

    def test_integrity_defaults_match_the_spec(self) -> None:
        assert get("integrity.phash_max_distance").default == 5
        assert get("integrity.phash_lookback_days").default == 30
        assert get("integrity.burst_window_minutes").default == 3
        assert get("integrity.photo_max_edge_px").default == 1600

    def test_ai_review_ships_disabled(self) -> None:
        """It has nothing to compare against until reference photos exist."""
        assert get("ai_review.enabled").default is False

    def test_no_business_date_key_exists(self) -> None:
        """The rollover is deliberately not a setting (docs/DECISIONS.md D9)."""
        assert not any("business_date" in k or "rollover" in k for k in REGISTRY)

    def test_every_key_names_its_own_entry(self) -> None:
        for key, definition in REGISTRY.items():
            assert definition.key == key

    def test_every_definition_has_label_and_description(self) -> None:
        for definition in REGISTRY.values():
            assert definition.label
            assert definition.description


class TestValidation:
    def test_number_range_is_enforced(self) -> None:
        d = get("scoring.weight.run_score")
        assert validate_value(d, 0.5) is None
        assert validate_value(d, 0) is None
        assert validate_value(d, 1) is None
        assert validate_value(d, 1.5) is not None
        assert validate_value(d, -0.1) is not None

    def test_number_rejects_non_numbers(self) -> None:
        d = get("scoring.weight.run_score")
        assert validate_value(d, "0.5") is not None
        assert validate_value(d, None) is not None
        # bool is an int subclass in Python; it must still be refused here.
        assert validate_value(d, True) is not None

    def test_integer_rejects_fractions(self) -> None:
        d = get("integrity.phash_max_distance")
        assert validate_value(d, 5) is None
        assert validate_value(d, 5.5) is not None

    def test_boolean(self) -> None:
        d = get("ai_review.enabled")
        assert validate_value(d, True) is None
        assert validate_value(d, False) is None
        assert validate_value(d, "true") is not None
        assert validate_value(d, 1) is not None

    def test_time(self) -> None:
        d = get("jobs.materialise_time")
        assert validate_value(d, "05:00") is None
        assert validate_value(d, "23:59") is None
        assert validate_value(d, "24:00") is not None
        assert validate_value(d, "5am") is not None
        assert validate_value(d, "05:60") is not None

    def test_string_choices(self) -> None:
        d = get("notifications.channel")
        assert validate_value(d, "email") is None
        assert validate_value(d, "log_only") is None
        assert validate_value(d, "carrier_pigeon") is not None

    def test_every_default_passes_its_own_validation(self) -> None:
        """A registry whose defaults fail its own rules is lying about one of
        the two."""
        for definition in REGISTRY.values():
            assert validate_value(definition, definition.default) is None, definition.key
