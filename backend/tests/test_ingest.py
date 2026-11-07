import pandas as pd
import pytest

from backend.ingest.loader import build_dataset
from backend.ingest.schemas import SCHEMAS


def test_every_synthetic_file_maps_to_its_domain(dataset):
    assert set(dataset.tables) == set(SCHEMAS)
    for report in dataset.files:
        assert report.domain is not None, report.filename
        assert report.unmapped == [], (report.filename, report.unmapped)
        assert report.missing_required == []


def test_recipe_ounces_and_milliliters_convert_to_base_units(dataset):
    rec = dataset.tables["recipes"]
    mozz = rec[(rec["item"] == "Margherita Pizza") & (rec["sku"] == "MOZZ")].iloc[0]
    assert mozz["unit"] == "lb"
    assert mozz["qty_per_item"] == pytest.approx(0.5)
    oil = rec[(rec["item"] == "Margherita Pizza") & (rec["sku"] == "OLIVE_OIL")].iloc[0]
    assert oil["unit"] == "l"
    assert oil["qty_per_item"] == pytest.approx(0.02)


def test_gallon_purchases_convert_quantity_and_price_together(dataset, restaurant):
    raw = restaurant.files["purchases.csv"]
    raw_oil = raw[raw["SKU"] == "OLIVE_OIL"].iloc[0]
    oil = dataset.tables["purchases"].query("sku == 'OLIVE_OIL' and not is_emergency").iloc[0]
    assert oil["unit"] == "l"
    raw_value = raw_oil["Qty Received"] * float(raw_oil["Unit Price"].lstrip("$"))
    assert oil["qty_received"] * oil["unit_price"] == pytest.approx(raw_value)


def test_quality_scores_are_high_for_clean_synthetic_data(dataset):
    for domain, q in dataset.quality.items():
        assert q.score > 0.9, domain


def test_duplicate_rows_within_a_file_are_kept_but_reuploads_are_dropped():
    csv = b"Reservation Date,Party Size,Status\n2026-05-01,2,Seated\n2026-05-01,2,Seated\n"
    ds = build_dataset([("reservations.csv", csv), ("reservations_copy.csv", csv)])
    assert len(ds.tables["reservations"]) == 2


def test_currency_strings_and_messy_headers_parse():
    csv = (b"Invoice Number,Date,Supplier,Item Code,Received,Price Per Unit\n"
           b"A1,2026-05-01,Sysco,FLOUR,10,\"$1,200.50\"\n")
    ds = build_dataset([("my_export.csv", csv)])
    p = ds.tables["purchases"]
    assert p["unit_price"].iloc[0] == pytest.approx(1200.50)
    assert pd.api.types.is_datetime64_any_dtype(p["invoice_date"])


def test_unrecognized_file_is_reported_not_crashing():
    ds = build_dataset([("notes.csv", b"foo,bar\n1,2\n")])
    assert ds.tables == {}
    assert ds.files[0].domain is None
