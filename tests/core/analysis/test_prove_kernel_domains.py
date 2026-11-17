"""Test the static facts that how() steers with."""

from __future__ import annotations

from pyrung.core import (
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    copy,
    math,
    on_delay,
    out,
)


class TestKernelDomainDiscovery:
    """Pilot sweep discovers finite domains for program-derived tags."""

    def test_timer_preset_source_excluded_from_comparison_only(self):
        """Tags used as timer preset sources are excluded from comparison-only absorption."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        enable = Bool("Enable", external=True)
        h1 = Int("H1", min=0, max=100)
        h2 = Int("H2", min=0, max=100)
        t1 = Timer.clone("T1")
        t2 = Timer.clone("T2")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(enable):
                copy(50, h1)
                copy(75, h2)
            with Rung(enable):
                on_delay(t1, preset=h1)
                on_delay(t2, preset=h2)
            with Rung(h1 > 30, h2 > 60):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "H1" not in comparison.comparison_tags
        assert "H2" not in comparison.comparison_tags

    def test_self_feeding_calc_is_excluded_from_comparison_only(self):
        """Check that a self-feeding math(X + N, X) keeps X out of the comparison tags.

        The math reads X through _reads, so _has_forbidden_data_read excludes X.
        """
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        boost = Bool("SelfFeedBoost", external=True)
        counter = Int("SelfFeedCounter", min=0, max=100)
        flag = Bool("SelfFeedFlag")

        with Program(strict=False) as logic:
            with Rung(boost):
                math(counter + 2, counter)
            with Rung(~boost):
                math(counter + 1, counter)
            with Rung(counter > 50):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "SelfFeedCounter" not in comparison.comparison_tags

    def test_mixed_self_feed_and_overwrite_writers_are_excluded(self):
        """A self-feeding calc next to an overwrite copy still reads the tag — excluded."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        boost = Bool("MixBoost", external=True)
        reset_btn = Bool("MixReset", external=True)
        counter = Int("MixCounter", min=0, max=100)
        flag = Bool("MixFlag")

        with Program(strict=False) as logic:
            with Rung(boost):
                math(counter + 3, counter)
            with Rung(~boost):
                math(counter + 1, counter)
            with Rung(reset_btn):
                copy(0, counter)
            with Rung(counter > 50):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "MixCounter" not in comparison.comparison_tags

    def test_structural_domains_boundary_seed_monotone_self_feed(self):
        """Preseeded monotone self-feed uses comparison boundaries instead of counting upward."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domain_info,
        )

        step = Int("CurStep")
        stored = Int("StoredStep")
        odd = Int("ValStepIsOdd")
        active = Bool("Active")

        with Program(strict=False) as logic:
            with Rung():
                math(step % 2, odd)
            with Rung(odd != 1):
                math(step + 1, step)
            with Rung(step != stored):
                copy(step, stored)
            with Rung(step == 3):
                out(active)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        domains, _blockers = _collect_structural_domain_info(
            logic,
            graph,
            all_exprs,
            literal_write_domains={"CurStep": (0, 1)},
        )

        assert domains["CurStep"] == (0, 1, 2, 3, 4)
        assert domains["StoredStep"] == (0, 1, 2, 3, 4)
        assert domains["ValStepIsOdd"] == (0, 1)

    def test_overwrite_only_tag_source_still_absorbs(self):
        """copy(Source, Stored) is overwrite-only — comparison-only absorption still applies."""
        from pyrung.core.analysis.pdg import build_program_graph
        from pyrung.core.analysis.prove.absorb import (
            _find_comparison_absorptions,
        )
        from pyrung.core.analysis.prove.classify import (
            _collect_all_exprs,
            _collect_structural_domains,
        )

        source = Int("OverwriteSource", external=True, min=0, max=300)
        stored = Int("OverwriteStored")
        flag = Bool("OverwriteFlag")

        with Program(strict=False) as logic:
            with Rung():
                copy(source, stored)
            with Rung(stored > 150):
                out(flag)

        graph = build_program_graph(logic)
        all_exprs = _collect_all_exprs(logic, graph)
        structural_domains = _collect_structural_domains(logic, graph, all_exprs)

        comparison = _find_comparison_absorptions(logic, graph, all_exprs, structural_domains)
        assert "OverwriteStored" in comparison.comparison_tags
