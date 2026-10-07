import pytest

from ds_web.cot import parse_cot_name


def test_parses_exchange_asset_category_position():
    parsed = parse_cot_name("Chicago Mercantile Exchange(CME)-Feeder Cattle Non-Commercial Short Index")
    assert parsed.exchange == "Chicago Mercantile Exchange"
    assert parsed.exchange_code == "CME"
    assert parsed.asset == "Feeder Cattle"
    assert (parsed.trader_category, parsed.trader_category_code) == ("Non-Commercial", "NC")
    assert parsed.position == "Short"
    assert parsed.futures_only is False


def test_mnemonic_settles_swap_dealer_ambiguity():
    name = "Chicago Board of Trade(CBT)-5YR Interest Rate Swap Dealer / Intermediary Spreading"
    parsed = parse_cot_name(name, "CNGDIXC")
    assert parsed.asset == "5YR Interest Rate Swap"
    assert parsed.trader_category_code == "DI"
    assert parsed.position == "Spreading"


def test_total_boilerplate_and_longest_category():
    name = ("Chicago Mercantile Exchange(CME)-Class III Milk Commodity futures Trading "
            "Commission(CFTC) Commitments of Traders(COT) Total Reportable Long")
    parsed = parse_cot_name(name)
    assert parsed.asset == "Class III Milk"
    assert parsed.trader_category == "Total Reportable"
    assert parsed.position == "Long"


def test_abbreviated_codes_and_zero_width_characters():
    assert parse_cot_name("ICE(IFE)-Soyabean Oil NR LG IT").position == "Long"
    parsed = parse_cot_name("NYMEX(NYM)-Crude Producer / Merchant / Proces​sor / User Short Futures Only")
    assert parsed.trader_category_code == "PM" and parsed.futures_only


@pytest.mark.parametrize("name", [None, "", "CME - Feeder Cattle Index", "FTSE 100"])
def test_rejects_non_cot_names(name):
    assert parse_cot_name(name) is None
