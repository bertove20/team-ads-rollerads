"""Biaya AI: perhitungan harga token dan rem darurat harian."""
import config
import llm


def test_harga_model_claude_dari_daftar_tetap():
    row = {"model": "claude-opus-5", "input_tokens": 1_000_000, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    assert llm.row_cost(row) == 5.0


def test_harga_model_openrouter_dari_daftar_harga_tersimpan(mem_storage):
    mem_storage[llm.OPENROUTER_PRICES_KEY] = {"openai/gpt-5.6-terra": [2.0, 12.0]}
    row = {"model": "openai/gpt-5.6-terra", "input_tokens": 1_000_000, "output_tokens": 1_000_000,
           "cache_read": 0, "cache_write": 0}
    assert llm.row_cost(row) == 14.0


def test_cache_jauh_lebih_murah():
    biasa = llm.row_cost({"model": "claude-opus-5", "input_tokens": 1_000_000, "output_tokens": 0,
                          "cache_read": 0, "cache_write": 0})
    cache = llm.row_cost({"model": "claude-opus-5", "input_tokens": 0, "output_tokens": 0,
                          "cache_read": 1_000_000, "cache_write": 0})
    assert cache < biasa / 5


def test_model_tak_dikenal_dipakai_harga_aman(mem_storage):
    row = {"model": "model/aneh", "input_tokens": 1_000_000, "output_tokens": 0, "cache_read": 0, "cache_write": 0}
    assert llm.row_cost(row) == 5.0


def test_rem_darurat_berhenti_saat_melewati_batas(monkeypatch):
    monkeypatch.setattr(config, "AI_DAILY_BUDGET_USD", 1.0)
    monkeypatch.setattr(llm, "cost_since", lambda ts: (1.5, []))
    llm._budget_cache["ts"] = 0
    assert llm.budget_exceeded() is True
    monkeypatch.setattr(llm, "cost_since", lambda ts: (0.4, []))
    llm._budget_cache["ts"] = 0
    assert llm.budget_exceeded() is False


def test_batas_nol_berarti_tanpa_batas(monkeypatch):
    monkeypatch.setattr(config, "AI_DAILY_BUDGET_USD", 0)
    monkeypatch.setattr(llm, "cost_since", lambda ts: (999.0, []))
    llm._budget_cache["ts"] = 0
    assert llm.budget_exceeded() is False


def test_setiap_pekerjaan_punya_nama_indonesia():
    for key in ("rapat", "laporan", "script", "landing", "ingatan", "tanya", "campaign"):
        assert key in llm.TASKS and llm.TASKS[key]
