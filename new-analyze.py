#! /usr/bin/env python
import json
import redis
import random
import msgpack
import sqlite3
import sys

from bitarray import bitarray, bitdiff
from collections import defaultdict
from optparse import OptionParser
from itertools import groupby

from cvgmeasure.conf import get_property, REDIS_URL_TG, REDIS_URL_OUT
from cvgmeasure.common import mk_key, tg_i_s, tn_i_s, i_tn_s
from cvgmeasure.d4 import get_num_bugs, iter_versions
from cvgmeasure.consts import ALL_TGS

def connect_db():
    return redis.StrictRedis.from_url(REDIS_URL_OUT)

def save_rows(r_out, qm, granularity, project, version, bases, pools, vals):

    # vals should be a dict from {'GRE:1': (fault_detection, determined_by, # selected, # pool, # base, runtime, pool_time, base_time,
    # )}'

    # redis key -> out:qm:granularity:project:version:base:pool
    # parts of the hash: algorithm:run_id =>

    r_out.hmset(mk_key('out', [qm, granularity, project, version, '.'.join(bases), '.'.join(pools)]), 
            {k: json.dumps(v) for k, v in vals.iteritems()})

RUNS = 30

class MissingTTs(Exception):
    pass


def get_essential_tests(bit_map, tests):
    unseen_tgs = bitarray(len(bit_map.itervalues().next()))
    unseen_tgs.setall(True)

    once_tgs = bitarray(len(bit_map.itervalues().next()))
    once_tgs.setall(False)

    for test in tests:
        t = bit_map[test]
        once_tgs = (unseen_tgs & t) | (once_tgs & ~t)
        unseen_tgs &= ~t

    return [test for test in tests if (once_tgs & bit_map[test]).any()]


subset_remember = defaultdict(dict)
def subset(t1, t2, ba1, ba2):
    subset_remember[t1][t2] = subset_remember[t1].get(t2, ba1 & ba2 == ba1)
    return subset_remember[t1][t2]

eq_remember = defaultdict(dict)
def eq(t1, t2, ba1, ba2):
    eq_remember[t1][t2] = eq_remember[t1].get(t2, ba1 == ba2)
    return eq_remember[t1][t2]

def get_redundant_tests(bit_map, tests):
    tg_counts = {test: bit_map[test].count() for test in tests}
    sorted_tests = sorted(tests, key= lambda x: tg_counts[x])

    redundant_tests = []
    for idx, i in enumerate(sorted_tests):
        for jidx in xrange(len(sorted_tests)-1, idx, -1):
            j = tests[jidx]
            if tg_counts[i] < tg_counts[j] and subset(i, j, bit_map[i], bit_map[j]):
                redundant_tests.append(i)
                break
    return redundant_tests


def get_equal_tests(bit_map, tests):
    tg_counts = {test: bit_map[test].count() for test in tests}
    sorted_tests = sorted(tests, key= lambda x: tg_counts[x])

    equal_tests = defaultdict(list) # { min_test -> set([eqs]) }
    eq_tests = set([])
    for idx, i in enumerate(sorted_tests):
        if i in eq_tests:
            continue
        for jidx in xrange(idx+1, len(sorted_tests)):
            j = sorted_tests[jidx]
            if tg_counts[i] < tg_counts[j]:
                break
            assert tg_counts[i] == tg_counts[j]
            if eq(i, j, bit_map[i], bit_map[j]):
                eq_tests.add(j)
                equal_tests[i].append(j)
    for k in equal_tests:
        equal_tests[k].append(k)

    redundant_tests = []
    for eqs in equal_tests.values():
        chosen_val = random.choice(eqs)
        redundant_tests.extend(x for x in eqs if x != chosen_val)
    return redundant_tests


def combinator(f1, f2):
    def combination(tg_map, tests):
        result = f1(tg_map, tests)
        remains = [t for t in tests if t not in result]
        result.extend(f2(tg_map, remains))
        return result
    return combination


def run_selection(bit_map, tests, initial_tests=[], verbose=False):
    chosen_tests = list(initial_tests)
    remaining_tests = set(tests)

    chosen_tgs = bitarray(len(bit_map.itervalues().next()))
    chosen_tgs.setall(False)
    for test in initial_tests:
        chosen_tgs |= bit_map[test]
        remaining_tests.remove(test)

    while True:
        max_additional_len = 0
        max_additional_tests = []

        empties = []
        for test in remaining_tests:
            additional_tgs = (bit_map[test] & ~chosen_tgs).count()
            if additional_tgs == 0:
                empties.append(test) # won't ever need to check this test again
            elif additional_tgs > max_additional_len:
                max_additional_tests = [test]
                max_additional_len = additional_tgs
            elif additional_tgs == max_additional_len:
                max_additional_tests.append(test)

        highest_tg_count_per_test, choices = max_additional_len, max_additional_tests
        if highest_tg_count_per_test == 0:
            break

        for empty in empties:
            remaining_tests.remove(empty)

        choice = random.choice(choices)
        if verbose:
            print len(chosen_tests), choice, len(choices), highest_tg_count_per_test

        chosen_tests.append(choice)
        chosen_tgs |= bit_map[choice]
        remaining_tests.remove(choice)

    return chosen_tests

import time
class Timer(object):
    def __init__(self):
        self.time = 0
        self.st_time = None

    def start(self):
        self.st_time = time.time()

    def stop(self):
        self.time += time.time() - self.st_time
        self.st_time = None

    @property
    def msec(self):
        return int(round(1000*self.time))


def greedy_minimization(all_tests, tts, bit_map, timing=lambda x: None, redundants=lambda tgs, tests: [], essentials=lambda tgs, tests: []):
        results = []
        tR, tE, tS = Timer(), Timer(), Timer()
        for i in xrange(RUNS):
                seed=7*i+13
                random.seed(seed)
                def do_one():
                    tR.start()
                    redundant_set = set(redundants(bit_map, all_tests))
                    non_redundant_tests = [test for test in all_tests if test not in redundant_set]
                    tR.stop()

                    tE.start()
                    essential_tests = essentials(bit_map, non_redundant_tests)
                    essential_tts = [tt for tt in tts if tt in essential_tests]
                    tE.stop()

                    tS.start()
                    selected = run_selection(bit_map, non_redundant_tests, initial_tests=essential_tests)
                    selected_set = set(selected)
                    selected_tts = tts & selected_set
                    tS.stop()
                    fault_detection = len(selected_tts) > 0

                    if all(tt in redundant_set for tt in tts):
                        determined_by = 'R'
                        assert not fault_detection
                    elif len(essential_tts) > 0:
                        determined_by = 'E'
                        assert fault_detection
                    else:
                        determined_by = 'S'
                    return (determined_by, 1 if fault_detection else 0, timing(selected), len(selected))

                results.append(do_one())
                print '{0}{1} ({3} TCs {2} ms)'.format(*results[-1]),
        print '...', len([r for _, r, _, _ in results if r > 0])
        print tR.msec, tE.msec, tS.msec
        return  results

def get_unique_goal_tts(tts, all_tests_set, tg_map):
    non_tt_tg_union = set([])
    for t in all_tests_set - tts:
        non_tt_tg_union |= tg_map[t]

    def get_unique_goals(tt):
        unique_goals = tg_map[tt] - non_tt_tg_union
        return unique_goals

    tts_with_unique_goals = [tt for tt in tts if len(get_unique_goals(tt)) > 0]
    return tts_with_unique_goals


def def_me(s):
    return {'name': s, 'granularity': 'file', 'fun': lambda s: s.startswith(s)}


def get_triggers_from_results(r, project, version, suite):
    result = json.loads(r.hget(mk_key('trigger', [project, version]), suite))
    return set(result)


def ALL(*args):
    return True

POOLS = {
    'B': (['dev'], lambda tidx, trigger_set: tidx not in trigger_set),
    'F': (['dev'], lambda tidx, trigger_set: tidx in trigger_set),
    'R': (['randoop.1'], ALL),
}

def minimization(conn, r, rr, qm_name, project, version, bases, augs):
    qm = QMS[qm_name]

    # 1. Get the testing goals that match the qm
    relevant_tgs = filter(qm['fun'], rr.hkeys(mk_key('tg-i', [project, version])))
    relevant_tg_is = tg_i_s(rr, relevant_tgs, project, version)
    relevant_tg_set = set(relevant_tg_is)

    tp_idx = {}
    idx_tp = []

    fails = {}
    def get_fails(suite, fails=fails):
        if not fails.has_key(suite):
            fail_ids = set(map(int, r.smembers(mk_key('fail', ['exec', project, version, suite]))))
            fails[suite] = fail_ids
        return fails[suite]

    def register_suite(suite, ff, tp_idx=tp_idx, idx_tp=idx_tp):
        tests = map(int, rr.hkeys(mk_key('tgs', [project, version, suite])))
        suite_triggers = get_triggers_from_results(r, project, version, suite)
        fails = get_fails(suite)
        # always remove failing tests
        tests_no_fail = filter(lambda x: x not in fails, tests)
        tests_filtered = filter(lambda x: ff(x, suite_triggers), tests_no_fail)
        triggers = filter(lambda x: x in suite_triggers, tests_filtered)
        print 'Suite: {0}, Tests: {1}, Suite Triggers:{2}, Selected Triggers: {3}, Fails: {4}, No fails: {5}, Filtered: {6}'.format(
                suite, *map(len, [tests, suite_triggers, triggers, fails, tests_no_fail, tests_filtered]))
        for test in tests_filtered:
            tp_idx[(suite, test)] = tp_idx.get((suite, test), len(idx_tp))
            if len(idx_tp) != len(tp_idx):
                idx_tp.append((suite, test))
        triggers = set(tp_idx[(suite, t)] for t in triggers)
        return triggers, [tp_idx[(suite, test)] for test in tests_filtered]


    # 2. Get the list of tests with some coverage goals from base / augmenting pools
    def flatten(l):
        return reduce(lambda a,b: a+b, l)

    def pool_to_tests(pools, tp_idx=tp_idx, idx_tp=idx_tp):
        tests = []
        triggers = set([])
        for pool in pools:
            pool_suites, pool_filter = POOLS[pool]
            for pool_suite in pool_suites:
                a, b = register_suite(pool_suite, pool_filter, tp_idx=tp_idx, idx_tp=idx_tp)
                tests.append(b)
                triggers.update(a)
        return triggers, flatten(tests)

    base_tp_idx = {}
    base_idx_tp = []
    base_triggers, base_tests = pool_to_tests(bases, base_tp_idx, base_idx_tp)

    aug_triggers, aug_tests = pool_to_tests(augs, tp_idx, idx_tp)

    print "Triggers: Base {0}, Aug {1}".format(len(base_triggers), len(aug_triggers))
    print "Reading..."
    tRead = Timer()
    tRead.start()

    def get_tg_map(tests, idx_tp, relevant_tg_set):
        get_suite = lambda t: idx_tp[t][0]
        get_tid   = lambda t: idx_tp[t][1]
        tests_sorted = sorted(tests, key=get_suite) # get suite
        tg_map = {}
        for suite, suite_tests_iter in groupby(tests_sorted, key=get_suite):
            suite_tests = list(suite_tests_iter)
            vals = rr.hmget(mk_key('tgs', [project, version, suite]), map(get_tid, suite_tests))
            vals_mapped = [set(msgpack.unpackb(v)) & relevant_tg_set for v in vals]
            tg_map.update(dict(zip(suite_tests, vals_mapped)))
        return {k: v for k, v in tg_map.iteritems() if len(v) > 0}

    base_tg_map = get_tg_map(base_tests, base_idx_tp, relevant_tg_set)
    get_all_tgs = lambda tg_map: reduce(lambda a, b: a | b, tg_map.values())
    get_all_tests = lambda tg_map: set(tg_map.keys())
    base_tgs = get_all_tgs(base_tg_map)
    print "Relevant_tgs {0}, Base tgs: {1}".format(*map(len, (relevant_tg_set, base_tgs)))

    base_relevant_tests = get_all_tests(base_tg_map)
    base_relevant_tts   = base_triggers & base_relevant_tests

    aug_relevant_tg_map = get_tg_map(aug_tests, idx_tp, relevant_tg_set)
    aug_tgs = get_all_tgs(aug_relevant_tg_map)
    aug_relevant_tests = get_all_tests(aug_relevant_tg_map)
    aug_relevant_tts = aug_triggers & aug_relevant_tests


    aug_additional_tg_map = get_tg_map(aug_tests, idx_tp, relevant_tg_set - base_tgs)
    aug_additional_tgs = get_all_tgs(aug_relevant_tg_map)
    aug_additional_tests = get_all_tests(aug_additional_tg_map)
    aug_additional_tts = aug_triggers & set(aug_additional_tests)

    print "Triggers: Base_relevant {0}, Aug_relevant {1}, Aug_additional {2}".format(*map(len,
            (base_relevant_tts, aug_relevant_tts, aug_additional_tts)))

    tRead.stop()
    print "Reading complete {0} msecs.".format(tRead.msec)

    tts_with_unique_goals = get_unique_goal_tts(aug_additional_tts, aug_additional_tests, aug_additional_tg_map)
    print "Guaranteed: ", len(tts_with_unique_goals)

    print "Building bit vectors"
    all_tgs = { k: i for i, k in enumerate(sorted(get_all_tgs(aug_additional_tg_map)))}
    def to_bitvector(s):
        bv = bitarray(len(all_tgs))
        bv.setall(False)
        for tg in s:
            bv[all_tgs[tg]] = 1
        return bv
    bit_map = {k : to_bitvector(v) for k, v in aug_additional_tg_map.iteritems()}
    print "Built.."



    def timing(tests, idx_tp=idx_tp):
        tps = map(lambda t: idx_tp[t], tests)
        tps_sorted = sorted(tps, key=lambda (suite, i): suite)
        for suite, i_it in groupby(tps_sorted, key=lambda(suite, i): suite):
            i_s = map(lambda (suite, i): i, i_it)
            if suite == 'dev':
                tns = i_tn_s(r, i_s, suite)
                tc_ns = set(tn.partition('::')[0] for tn in tns)
                tc_is = tn_i_s(r, list(tc_ns), suite, allow_create=False)
                all_is = tc_is + i_s
            else:
                all_is = i_s
            method_times = [msgpack.unpackb(b) for b in r.hmget(mk_key('time', [project, version, suite]), all_is)]
            def aggr(l):
                if any(x == -1 for x in l):
                    raise Exception('bad timing for tests')

                # let's go with average
                return reduce(lambda a, b: a+b, l)/len(l)
            method_times_aggregate = [aggr(timings) for timings in method_times]
            suite_time = reduce(lambda a, b: a+b, method_times_aggregate)
            return suite_time


    base_time = timing(base_tests, base_idx_tp)
    def timing_with_base(tests, idx_tp=idx_tp):
        return timing(tests, idx_tp) + base_time

# row independent info
    info = (
                len(relevant_tgs),
                len(base_triggers), len(base_tests), len(base_tgs), base_time,   # all of base suite
                len(base_relevant_tts), len(base_relevant_tests), len(base_tgs), timing(base_relevant_tests, base_idx_tp), # relevant part of base suite
                len(aug_tests), len(aug_triggers), timing(aug_tests), len(aug_tgs),                # all of augmentation pool
                len(aug_relevant_tests), len(aug_relevant_tts), timing(aug_relevant_tests), len(aug_tgs), # relevant part of aug pool
                len(aug_additional_tests), len(aug_additional_tts), timing(aug_additional_tests), len(aug_additional_tgs), # part of aug pool in addition to the base suite
            )
    print info
    raise Exception('done')
#   qm, granularity, project, version, base, select_from, algorithm, run_id, fault_detection, determined_by)
    minimization = lambda **kwargs: greedy_minimization(list(aug_additional_tests), aug_additional_tts, bit_map, timing_with_base, **kwargs)
    algo_params = [
        ('G', {}),
        ('GE', {'esessentials': get_essential_tests}),
        ('GRE', {'redundants': get_redundant_tests, 'essentials': get_essential_tests}),
        ('GREQ', {'redundants': combinator(get_redundant_tests, get_equal_tests), 'essentials':get_essential_tests}),
    ]
    algos = [(name, lambda: minimization(**algo_param)) for (name, algo_param) in algo_params]
    for algo, fun in algos:
        results = fun()
# vals should be a dict from {'GRE:1': (fault_detection, determined_by, # selected, # pool, # base, runtime, pool_time, base_time, }
        save_rows(conn, qm['name'], qm['granularity'], project, version, bases, augs,
                {'{algo}:{run}'.format(algo=algo, run=run_id+1): (result, determined_by if len(tts_with_unique_goals) == 0 else 'U',
                    count,
                    )
                        for (run_id, (determined_by, result, time, count)) in enumerate(results)}
        )


QMS = {
        'line': def_me('line'),
        'line:cobertura': def_me('branch:cobertura'),
        'line:codecover': def_me('branch:codecover'),
        'line:jmockit': def_me('line:jmockit'),

        'branch': def_me('branch'),
        'branch:cobertura': def_me('branch:cobertura'),
        'branch:codecover': def_me('branch:codecover'),
        'branch:jmockit': def_me('branch:jmockit'),

        'branch-line': {'name': 'branch-line', 'granularity': 'file', 'fun': lambda s: s.startswith('branch:') or s.startswith('line:')},

        'statement-line': {'name': 'statement-line', 'granularity': 'file', 'fun': lambda s: s.startswith('statement:') or s.startswith('line')},
        'statement:codecover': def_me('statement:codecover'),

        'data': {'name': 'data', 'granularity': 'file', 'fun': lambda s: s.startswith('data:')},

        'branch-loop-line': {'name': 'branch-loop', 'granularity': 'file', 'fun': lambda s: s.startswith('branch:') or s.startswith('loop:') or s.startswith('line:')},
        'branch-loop-path-line': {'name': 'branch-loop-path', 'granularity': 'file', 'fun': lambda s: s.startswith('branch:') or s.startswith('loop:') or s.startswith('path:') or s.startswith('line:')},

        'mutcvg': {'name': 'mutcvg', 'granularity': 'file', 'fun': lambda s: s.startswith('mutcvg:')},
        'mutcvg-line': {'name': 'mutcvg', 'granularity': 'file', 'fun': lambda s: s.startswith('mutcvg:') or s.startswith('line:')},
        'mutant': {'name': 'mutant', 'granularity': 'file', 'fun': lambda s: s.startswith('mutant:')},
        'mutant-line': {'name': 'mutant-line', 'granularity': 'file', 'fun': lambda s: s.startswith('mutant:') or s.startswith('line:')},
    }

def main(options):
    r = redis.StrictRedis.from_url(get_property('redis_url'))
    rr = redis.StrictRedis.from_url(REDIS_URL_TG)
    conn = connect_db()

    for qm in sorted(['line']):
        for project, v in iter_versions(restrict_project=options.restrict_project, restrict_version=options.restrict_version):
            print "----( %s %d --  %s )----" % (project, v, qm)
            minimization(conn, r, rr, qm, project, v, ['B'], ['R','F'])
            print

if __name__ == "__main__":
    parser = OptionParser()
    parser.add_option("-p", "--project", dest="restrict_project", action="append", default=[])
    parser.add_option("-v", "--version", dest="restrict_version", action="append", default=[])
    (options, args) = parser.parse_args(sys.argv)
    main(options)
