#!/usr/bin/env python3
"""Profil s anglickými názvami (izby, entity ESP, zariadenie) pre test_en_kompletne.py: python en_profile.py <výstup.json>"""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def english_profile():
    import profil
    p = profil.default()
    p["zariadenie"]["nazov"] = "Samsung EHS Split 12 kW (AE120JXEDGH + AE160JNYDGH)"
    p["esp"]["entity"] = {
        "vonku": "Outdoor temperature", "sledovana": "Tracked temperature (Nursery)", "ciel_akt": "Current room target temperature",
        "posun": "Heating curve offset", "wl": "Target water temperature (WL)", "tw2": "Outlet water temperature (Tw2)", "tw1": "Return (Tw1)",
        "dt": "Heating water Delta T", "freq": "Compressor frequency", "prikon": "Estimated heat pump power", "tuv": "DHW temperature (Boiler)",
        "tuv_ciel": "DHW target temperature", "pwm": "Circulation pump speed (PWM)", "chyba": "Heat pump error code", "kwh_den": "Daily heat pump consumption",
        "flow": "Water flow", "ventil": "3-way valve (Heating direction)", "odmrazovanie": "Defrost", "termostat": "Thermostat (heat demand)",
        "ciel_izby": "Room target temperature"}
    p["esp"]["nastavenia"] = {
        "fsv2011": "FSV 2011 Coldest outdoor", "fsv2012": "FSV 2012 Warmest outdoor", "fsv2021": "FSV 2021 WL1 water at coldest",
        "fsv2022": "FSV 2022 WL1 water at warmest", "fsv2031": "FSV 2031 WL2 water at coldest", "fsv2032": "FSV 2032 WL2 water at warmest",
        "fsv4013": "FSV 4013 Heating off above outdoor temp.", "max_posun": "Maximum positive curve offset",
        "max_posun_zaporny": "Maximum curve offset", "zosilnenie": "Curve offset gain"}
    p["esp"]["rest_extra"] = ["Radiator Dining room (head)"]
    names = {"detska": ("Nursery", "Room Nursery (Zigbee)"), "jedalen": ("Dining room", "Room Dining room"), "kuchyna": ("Kitchen", "Room Kitchen"),
             "obyvacka": ("Living room", "Room Living room"), "spalna": ("Bedroom", "Room Bedroom"), "pracovna": ("Study", "Room Study"),
             "kupelna": ("Bathroom", "Room Bathroom")}
    for r in p["izby"]:
        r["nazov"], r["esp"] = names[r["kluc"]]
    return p


if __name__ == "__main__":
    with open(sys.argv[1], "w", encoding="utf-8") as f:
        json.dump(english_profile(), f, ensure_ascii=False, indent=1)
