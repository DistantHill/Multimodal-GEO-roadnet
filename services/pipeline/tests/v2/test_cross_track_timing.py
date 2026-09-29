from track.v2.candidates.cross_track_timing import CrossTrackTiming


def test_timed_iterator_excludes_consumer_and_closes_source(monkeypatch):
    clock = [0.]
    monkeypatch.setattr('track.v2.candidates.cross_track_timing.perf_counter', lambda: clock[0])
    closed = []
    def source():
        try:
            clock[0] += 2.
            yield 1
            clock[0] += 3.
        finally:
            closed.append(True)
    timing = CrossTrackTiming()
    iterator = timing.iterate(source(), 'candidate_s')
    assert next(iterator) == 1
    clock[0] += 100.
    assert list(iterator) == []
    assert timing.values['candidate_s'] == 5.
    assert closed == [True]
    iterator = timing.iterate(source(), 'candidate_s')
    assert next(iterator) == 1
    iterator.close()
    assert closed == [True, True]
