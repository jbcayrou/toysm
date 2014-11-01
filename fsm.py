# pylint: disable=unexpected-keyword-arg, no-value-for-parameter,star-args
# pylint: disable=invalid-name

try:
    # python3
    import queue
except ImportError:
    # python2
    # pylint: disable=import-error
    import Queue as queue
import sched
import time
import subprocess
from threading import Thread
import sys

from core import *

import logging
LOG = logging.getLogger(__name__)

DOT = 'dot'
XDOT = 'xdot'

def _bytes(string, enc='utf-8'):
    '''Returns bytes of the string argument. Compatible w/ Python 2
       and 3.'''
    if sys.version_info.major < 3:
        return string
    else:
        return bytes(string, enc)

def dot_attrs(obj, **overrides):
    if overrides:
        d = obj.dot.copy()
        d.update(overrides)
    else:
        d = obj.dot
    def resolve(item):
        k, v = item
        if callable(v):
            v = v(obj)
        v = str(v)
        if not(v.startswith('<') and v.endswith('>')):
            v = '"%s"' % v.replace('"', r'\"')
        return k, v
    return ';'.join('%s=%s' % (k, v)
                    for (k, v) in (resolve(i) for i in d.items()))



class StateMachine:
    '''StateMachine .... think of something smart to put here ;-).'''
    MAX_STOP_WAIT = .1

    def __init__(self, cstate, *states):
        if states:
            self._cstate = State()
            self._cstate.add_state(cstate, initial=True)
            for s in states:
                self._cstate.add_state(s)
        elif isinstance(cstate, State):
            self._cstate = cstate
        else:
            # Builder expression is wrapped into a superstate
            self._cstate = State(sub=cstate)
        self._event_queue = queue.Queue()
        self._completed = set() # set of completed states
        if sys.version_info.major < 3:
            self._sched = sched.scheduler(time.time, self._sched_wait)
            self._v3sched = False
        else:
            self._sched = sched.scheduler()
            self._v3sched = True
        self._terminated = False
        self._thread = None

    def start(self):
        '''Starts the StateMachine.'''
        if self._thread:
            raise Exception('State Machine already started')
        self._terminated = False
        #self._thread = Thread(target=self._loop, daemon=True)
        self._thread = Thread(target=self._loop)
        self._thread.daemon = True
        self._thread.start()

    def join(self, *args):
        '''Joins the StateMachine internal thread.
           The method will return once the StateMachine has terminated.
        '''
        t = self._thread
        if t is not None:
            t.join(*args)

    def pause(self):
        '''Request the StateMachine to pause.'''
        pass

    def stop(self):
        '''Stops the State Machine.'''
        LOG.debug("%s - Stopping state machine", self)
        self._terminated = True

    def post(self, *evts):
        '''Adds an event to the State Machine's input processing queue.'''
        for e in evts:
            self._event_queue.put(e)

    def post_completion(self, state):
        '''Indicates to the SM that the state has completed.'''
        if state is None:
            self._terminated = True
        else:
            LOG.debug('%s - state completed', state)
            self._completed.add(state)

    def _assign_depth(self, state=None, depth=0):
        '''Assign _depth attribute to states used by the StateMachine.
           Depth is 0 for the root of the graph and each level of
           ancestor between a state and the root adds 1 to its depth.
        '''
        state = state or self._cstate
        state._depth = depth        # pylint: disable = W0212
        for c in state.children:
            self._assign_depth(c, depth + 1)

    def _lca(self, a, b):
        '''Returns paths to least common ancestor of states a and b.'''
        if a is b:
            return [a], [b] # LCA found
        if a._depth < b._depth:     # pylint: disable = W0212
            a_path, b_path = self._lca(a, b.parent)
            return a_path, b_path + [b]
        elif b._depth < a._depth:   # pylint: disable = W0212
            a_path, b_path = self._lca(a.parent, b)
            return [a] + a_path, b_path
        else:
            a_path, b_path = self._lca(a.parent, b.parent)
            return [a] + a_path, b_path + [b]

    def _sched_wait(self, delay):
        '''Waits for next scheduled event all the while processing
           potential external events posted to the SM.

           This method is used with the python2 scheduler that
           doesn't support the non-blocking call to run().'''
        t_wakeup = time.time() + delay
        while not self._terminated:
            t = time.time()
            if t >= t_wakeup:
                break
            # resolve all completion events in priority
            self._process_completion_events()
            self._process_next_event(t_wakeup)

    def _process_completion_events(self):
        '''Processes all available completion events.'''
        while not self._terminated and self._completed:
            state = self._completed.pop()
            LOG.debug('%s - handling completion of %s', self, state)
            self._step(evt=None,
                       transitions=state.get_enabled_transitions(None))
            if state.parent:
                state.parent.child_completed(self, state)
            else:
                state._exit(self)   #pylint: disable=protected-access
                self.stop() # top level region completed.

    def _process_next_event(self, t_max=None):
        '''Process events posted to the SM until a specified time.'''
        while not self._terminated:
            try:
                if t_max:
                    t = time.time()
                    if t >= t_max:
                        break
                    else:
                        delay = min(t_max - t, self.MAX_STOP_WAIT)
                else:
                    delay = self.MAX_STOP_WAIT
                evt = self._event_queue.get(True, delay)
                self._step(evt)
                break
            except queue.Empty:
                continue

    def _loop(self):
        # assign dept to each state (to assist LCA calculation)
        self._assign_depth()

        # perform entry into the root region/state
        self._cstate._enter(self)       # pylint: disable = W0212
        entry_transitions = self._cstate.get_entry_transitions()
        self._step(evt=None, transitions=entry_transitions)

        # loop should:
        # - exit when _terminated is True
        # - sleep for MAX_STOP_WAIT at a time
        # - wakeup when an event is queued
        # - wakeup when a scheduled task needs to be performed
        LOG.debug('%s - beginning event loop', self)
        while not self._terminated:
            # resolve all completion events in priority
            self._process_completion_events()
            if self._v3sched:
                tm_next_sched = self._sched.run(blocking=False)
                if tm_next_sched:
                    tm_next_sched += time.time()
                self._process_next_event(tm_next_sched)
            else:
                if not self._sched.empty():
                    self._sched.run()
                else:
                    self._process_next_event()
            LOG.debug('%s - end of loop, remaining events %r',
                      self, self._event_queue.queue)
        self._thread = None

    def _step(self, evt, transitions=None):
        LOG.debug('%s - processing event %r', self, evt)
        if transitions is None:
            transitions = self._cstate.get_enabled_transitions(evt)
        while transitions:
            #pylint: disable=protected-access
            #t, *transitions = transitions   # 'pop' a transition
            t, transitions = transitions[0], transitions[1:]
            LOG.debug("%s - following transition %s", self, t)
            if t.kind is Transition.INTERNAL:
                t._action(self, evt)    # pylint: disable = W0212
                continue
            src = t.source
            tgt = t.target or t.source #if no target is defined, target is self
            s_path, t_path = self._lca(src, tgt)
            if src is not tgt \
                and t.kind is not Transition._ENTRY \
                and isinstance(s_path[-1], ParallelState):
                raise Exception("Error: transition from %s to %s isn't allowed "
                                "because source and target states are in "
                                "orthogonal regions." %
                                (src, tgt))
            if t.kind is Transition.EXTERNAL \
                and (len(s_path) == 1 or len(t_path) == 1):
                s_path[-1]._exit(self)
                t_path.insert(0, None)
            elif len(s_path) > 1:
                s_path[-2]._exit(self)

            LOG.debug('%s - performing transition behavior for %s', self, t)
            t._action(self, evt)

            for a, b in [(t_path[i], t_path[i+1])
                         for i in range(len(t_path) - 1)]:
                if a is not None:
                    a.active_substate = b
                b._enter(self)

            transitions = tgt.get_entry_transitions() + transitions
        LOG.debug("%s - step complete for %r", self, evt)

    def graph(self, fname=None, fmt=None, prg=None):
        '''Generates a graph of the State Machine.'''

        def write_node(stream, state, transitions=None):
            transitions.extend(state.transitions)
            if state.parent and isinstance(state.parent, ParallelState):
                attrs = dot_attrs(state, shape='box', style='dashed')
            else:
                attrs = dot_attrs(state)
            if state.children:
                stream.write(_bytes('subgraph cluster_%s {\n' % id(state)))
                stream.write(_bytes(attrs + "\n"))
                if state.initial \
                    and not isinstance(state.initial, InitialState):
                    i = InitialState()
                    write_node(stream, i, transitions)
                    # pylint: disable = protected-access
                    transitions.append(Transition(source=i,
                                                  target=state.initial,
                                                  kind=Transition._ENTRY))

                for c in state.children:
                    write_node(stream, c, transitions)
                stream.write(b'}\n')
            else:
                stream.write(_bytes('%s [%s]\n' % (id(state), attrs)))

        def find_endpoint_for(node):
            if node.children:
                if node.initial:
                    node = node.initial
                else:
                    node = list(node.children)[0]
                return find_endpoint_for(node)
            else:
                return id(node)

        if fname:
            fmt = fmt or (fname[-3:] if fname[-4:-3] == '.' else 'svg')
            cmd = "%s -T%s > %s" % (prg or DOT, fmt, fname)
        else:
            cmd = prg or XDOT

        # Go through all states and generate dot to create the graph
        #with subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE) as proc:
        proc = subprocess.Popen(cmd, shell=True, stdin=subprocess.PIPE)
        try:
            f = proc.stdin
            transitions = []
            f.write(b"digraph { compound=true; edge [arrowhead=vee]\n")
            write_node(f, self._cstate, transitions=transitions)
            for t in transitions:
                src, tgt = t.source, t.target or t.source
                attrs = {}
                if src.children:
                    attrs['ltail'] = "cluster_%s" % id(src)
                src = find_endpoint_for(src)
                if tgt.children:
                    attrs['lhead'] = "cluster_%s" % id(tgt)
                tgt = find_endpoint_for(tgt)
                f.write(_bytes('%s -> %s [%s]\n' %
                               (src, tgt, dot_attrs(t, **attrs))))
            f.write(b"}")
            f.close()
        finally:
            proc.wait()


if __name__ == "__main__":
    from transition import Timeout
    from state import FinalState, HistoryState
    #s = State()
    #s1 = State('s1', parent=s, initial=True)
    #s2 = State('s2', parent=s)
    #f = FinalState(parent=s)

    #s1 > s2 > Transition(lambda e:True, lambda sm,e:None) > f

    state_s1 = State('s1')
    state_s2 = ParallelState('s2')
    state_fs = FinalState()

    state_s21 = State('s21', parent=state_s2)
    state_s22 = State('s22', parent=state_s2)

    state_s211 = State('s211', parent=state_s21, initial=True)
    state_s221 = State('s221', parent=state_s22, initial=True)

    state_s0 = State('s0')
    state_h = HistoryState(parent=state_s0)
    state_s0.add_state(state_s1, initial=True)
    state_s0.add_state(state_s2)
    state_s0.add_state(state_fs)
    state_machine = StateMachine(state_s0)
    #sm = fsm.StateMachine(s1, s2, fs)


    state_s1 >> 'a' >> state_s2 >> 'b' >> state_fs

    state_s1 >> Timeout(10) >> state_fs

    state_machine.graph()

# vim:expandtab:sw=4:sts=4
