import datetime
from collections import (
    defaultdict,
    deque,
)
from functools import reduce
from typing import (
    cast,
    Deque,
    Dict,
    List,
    Iterable,
    Iterator,
    Optional,
    Set,
    Tuple,
    Union,
)

from bnf_to_cnf.parser import (  # type: ignore
    Parser,
    NodeType,
    Node,
)
from .firstset import FirstSet
from .followset import FollowSet
from .production import Production
from .grammar import Grammar
from .subproduction import SubProduction
from .debug import RecurseDebug
from .symbols import (
    is_term,
    is_nonterm,
    is_epsilon,
)
from .utils import gen_cache


class LLTableGenerator:
    def __init__(
        self, grammar: str, lookahead: int = 1, debug: Set[str] = set()
    ) -> None:
        self.lookahead = lookahead
        self.bnf = Parser().parse(grammar)
        self._table = None  # type: Optional[List[Production]]
        self._adjacency_matrix = (
            None
        )  # type: Optional[Dict[str, List[List[str]]]]  # noqa
        self.start = next(self.bnf.filter(Node.is_start)).value
        self.fi_debug = RecurseDebug("fi") if "kfirst" in debug else None
        self.fo_debug = RecurseDebug("fo") if "kfollow" in debug else None

    @property
    def terminals(self) -> Iterable[str]:
        for _, lhs in self.table:
            for term in lhs:
                if self._is_term(term):
                    yield term

    @property
    def nonterminals(self) -> Iterable[str]:
        for rhs, _ in self.table:
            yield rhs

    def _normalize_terminal_value(self, value: str) -> str:
        return value.replace("\\", "")

    @property
    def adjacency_matrix(self) -> Dict[str, List[List[str]]]:
        """Get the grammar as an adjacency matrix.

        Returns:
            An adjacency matrix, where each non-terminal points to
            the productions it creates.

        """
        if self._adjacency_matrix:
            return self._adjacency_matrix
        self._adjacency_matrix = dict()
        for lhs, rhs in self.table:
            if lhs not in self._adjacency_matrix:
                self._adjacency_matrix[lhs] = list()
            self._adjacency_matrix[lhs].append(rhs)
        return self._adjacency_matrix

    @property
    def table(self) -> List[Production]:
        """Transform the grammar into an easier representation for parsing.

        Returns:
            The grammar, but as a list of productions.  For example,
            the grammar

                <S> ::= <A>
                    | <A> <B>
                <A> ::= "A"
                <B> ::= "B"

            would be represented as

                [ ('S', ['A'])
                , ('S', ['A', 'B'])
                , ('A', ['"A"'])
                , ('B', ['"B"'])
                ]

        """
        if self._table:
            return self._table
        self._table = list()
        for production in self.bnf.filter(
            lambda x: x.node_type == NodeType.PRODUCTION
        ):
            assert production.children[0].node_type == NodeType.SYMBOL
            lhs = production.children[0].value
            for expression in production.filter(
                lambda x: x.node_type == NodeType.EXPRESSION
            ):
                for sequence in expression.filter(
                    lambda x: x.node_type == NodeType.SEQUENCE,
                ):
                    rhs = list()  # type: List[str]
                    for child in sequence.children:
                        if child.node_type == NodeType.SYMBOL:
                            rhs.append(child.value)
                        elif child.node_type == NodeType.TERMINAL:
                            rhs.append(
                                self._normalize_terminal_value(child.value)
                            )
                    self._table.append((lhs, rhs))
        return self._table

    def _is_term(self, symbol: str) -> bool:
        return symbol.startswith('"') and symbol.endswith('"')

    def _eps(self, grammar: List[Production]) -> Iterable[Production]:
        for lhs, rhs in grammar:
            if len(rhs) == 1 and rhs[0] == "ε":
                yield (lhs, rhs)

    def _terms(self, grammar: List[Production]) -> Iterable[Production]:
        for lhs, rhs in grammar:
            if len(rhs) > 0 and self._is_term(rhs[0]):
                yield (lhs, rhs)

    def _nonterms(self, grammar: List[Production]) -> Iterable[Production]:
        """Yield all of the non-terminal productions in the grammar.

        Args:
            grammar: The grammar to extract non-terminals from.

        Yields:
            Productions which are non-terminal.

        """
        for lhs, rhs in grammar:
            if len(rhs) > 0 and not self._is_term(rhs[0]) and rhs[0] != "ε":
                yield (lhs, rhs)

    def _after(
        self, needle: str, haystack: List[str]
    ) -> Iterable[Tuple[str, Optional[str]]]:
        gen = (x for x in haystack)
        try:
            while True:
                symbol = next(gen)
                if symbol == needle:
                    s1 = next(gen)
                    try:
                        s2 = next(gen)
                    except StopIteration:
                        yield s1, None
                        break
                    yield s1, s2
        except StopIteration:
            pass

    def two_iter(self, rhs: List[str]) -> Iterable[Tuple[str, str]]:
        for i in range(len(rhs) - 1):
            yield rhs[i], rhs[i + 1]

    def reverse_nonterms(
        self, rhs: List[str], first: Dict[str, Set[str]]
    ) -> Iterable[str]:
        """Return production-end non-terminals.

        Args:
            rhs: The right-hand side.
            first: The first set.

        Yields:
            Non-terminals at the end of the production.  Yields
            starting at the last one, and continues until it reaches
            a non-ε terminal or non-terminal without ε in its first
            set.

        """
        for i in range(len(rhs))[::-1]:
            if self._is_term(rhs[i]):
                break
            elif rhs[i] == "ε":
                continue
            else:
                if "ε" not in first[rhs[i]]:
                    yield rhs[i]
                    break
                else:
                    yield rhs[i]

    @gen_cache(max_iterator_depth=10000)
    def _kfirst(
        self,
        symbol: Union[str, SubProduction],
        k: int,
        allow_underflow: bool = True,
        parent_debug: Optional[str] = None,
    ) -> Iterable[FirstSet]:
        """Get the k-first lookup table.

        This method satisfies

            _kfirst(k) = ⋃ Fi(s0, n) ∀ n ∊ 1..k

        Where `s0` is a symbol in the grammar and

            Fi(s0, n) = ∀ p ∊ G, the set of all
                | ⟨x1⟩
                |   if p = s0 -> x1 where x1 is terminal or ε, and n = 1
                | ⟨x1, x2, ..., xn⟩
                |   if s0 -> x1 x2 ... xn, ... is in G
                |       where x1..xn are terminals
                | ⟨x1, x2, ..., xj⟩ × Fi(⟨s1, ...⟩, n - j)
                |   if s0 -> x1 x2 ... xj, s1, ..., where x1-xn are terminals,
                |   s1 is non-terminal, and Fi(s1, n - j) is defined.

        The symbol ε counts as length 1, and could be found in the
        middle of any of the subproductions in the yielded sets.
        These are valid, even if they're not of the correct length.

        Args:
            symbol: The lhs or rhs for which we should generate kfirst.
            k: The maximum number of symbols in the yielded firstset.
            allow_underflow: Whether we can yield firstsets of less than
                k length.
            parent_debug: A debug argument which allows us to capture calls
                to this algorithm, and from which, we can print out a
                graphviz graph.

        Yields:
            Lists of terminal symbols.

        """
        debug_symbol = None
        if self.fi_debug:
            debug_symbol = self.fi_debug.add_call(
                [symbol, k], {"allow_underflow": allow_underflow}, extra=[]
            )
        if debug_symbol and parent_debug and self.fi_debug:
            self.fi_debug.add_child(parent_debug, debug_symbol)

        G = Grammar(self.table)

        # Fi(S, k)
        if isinstance(symbol, str):
            for subproduction in G[symbol]:
                yield from self._kfirst(
                    subproduction,
                    k,
                    allow_underflow=allow_underflow,
                    parent_debug=debug_symbol,
                )
            return

        assert isinstance(symbol, SubProduction)
        # Fi(<>, k)
        if not symbol:
            if self.fi_debug and debug_symbol:
                self.fi_debug.add_result(debug_symbol, "<>")
            yield FirstSet()
            return

        # Fi(<s1, s2, ..., s_k>, k)
        terms, rest = symbol.initial_terminals(k)
        # There will be more than k terms if k = 0 and symbol contains ε.
        if len(terms) == 1 and terms[0] == "ε" and k == 0:
            if self.fi_debug and debug_symbol:
                self.fi_debug.add_result(debug_symbol, terms[0])
            yield FirstSet(terms)
            return
        if terms and len(terms) == k:
            if allow_underflow or len(rest) == 0:
                if self.fi_debug and debug_symbol:
                    self.fi_debug.add_result(debug_symbol, terms)
                yield FirstSet(terms)
                return
        # len(terms) < k

        # Fi(<s1, s2, ..., s_(k - n)>, k), where n > 0
        # We can't build up to k symbols.
        if not rest:
            if self.fi_debug and debug_symbol:
                self.fi_debug.add_result(debug_symbol, "X")
            return

        # Fi(<S, s2, ...>, k)
        # The first symbol is non-terminal.
        if len(terms) == 0:
            head, rest = symbol.head()
            assert head is not None
            yield from self._kfirst(
                head, k, allow_underflow, parent_debug=debug_symbol
            )
            # At i = k, first_first will be _kfirst(head, 0)
            # This is meaningful if head has <ε>.
            for i in range(1, k + 1):
                for first_first in self._kfirst(
                    head, k - i, False, parent_debug=debug_symbol
                ):
                    for second_first in self._kfirst(
                        rest, i, allow_underflow, parent_debug=debug_symbol
                    ):
                        if self.fi_debug and debug_symbol:
                            self.fi_debug.add_result(
                                debug_symbol, first_first * second_first
                            )
                        yield first_first * second_first
            return

        # Fi(<s1, s2, ..., s_(k - n), S, ...>, k), where n > 0
        # There are < k terminals, followed by at least one non-terminal.
        rest_kfirst = self._kfirst(
            rest, k - len(terms), allow_underflow, parent_debug=debug_symbol
        )
        rest_kfirst = reduce(FirstSet.__or__, rest_kfirst, FirstSet())
        if debug_symbol and self.fi_debug:
            self.fi_debug.add_result(
                debug_symbol, FirstSet(terms) * rest_kfirst
            )
        yield FirstSet(terms) * rest_kfirst

    def kfirst(self, k: int) -> Dict[str, Set[Union[str, Tuple[str, ...]]]]:
        F = {
            x: set() for x, _ in self.table
        }  # type: Dict[str, Set[Union[str, Tuple[str, ...]]]]
        for i in range(1, k + 1):
            for symbol in F.keys():
                first_set = reduce(
                    FirstSet.__or__, self._kfirst(symbol, i), FirstSet()
                )
                for subproduction in first_set.sequences:
                    normalized = subproduction.normalized()
                    if len(normalized) == 1:
                        F[symbol].add(normalized[0])
                    else:
                        # If the subproductions were just epsilons, add an
                        # epsilon.
                        if not normalized:
                            F[symbol].add("ε")
                        else:
                            F[symbol].add(tuple(normalized))
        return F

    def first(self) -> Dict[str, Set[str]]:
        """Calculate the first set for generating an LL(1) table.

        Returns:
            A mapping from non-terminal productions to the first terminal
            symbols (or ε), which can occur in them.

        """
        return {
            key: {x for x in value if isinstance(x, str)}
            for key, value in self.kfirst(1).items()
        }

    def _kfollow_resolve_permutation(
        self,
        production: Production,
        base_index: int,
        current_permutation: Tuple[int, ...],
        allow_firstset: bool = True,
        parent_debug: Optional[str] = None,
    ) -> Iterable[SubProduction]:
        """Get all derivations from this subproduction of the exact lengths.

        Args:
            production: The production from which we will take derivations.
            base_index: How far into the rhs the derivations should start.
                That is, our derivation will be for production[1][base_index:].
            current_permutation: The exact lengths we are searching for.  For
                example, if current_permutation is [1, 0, 2], our production
                is ('A', ['a', 'B', 'C', 'D']), and the base index is 1, then
                we are seeking all derivations of ['B', 'C', 'D'] such that the
                derivation of 'B' is of length 1, the derivation of 'C' is a
                epsilon, and the derivation of 'D' is of length 2.
            allow_firstset: If true, then we should allow the returned
                subproductions to include values taken from the firstset of a
                production.
            parent_debug: If we're debugging the algorithm, the debug
                information of the parent call.

        Yields:
            SubProductions which follow the given permutation's length
            requirements.

        """
        debug = None
        if self.fo_debug and parent_debug:
            debug = self.fo_debug.add_call(
                [production, base_index, current_permutation],
                dict(),
                ["_kfollow_resolve_permutations"],
            )
            self.fo_debug.add_child(parent_debug, debug)
        lhs, rhs = production
        assert base_index < len(rhs), (
            f"The permutation should have some symbols, but it starts "
            f"at {base_index} for {rhs}"
        )

        last_nonzero = len(current_permutation) - 1
        while last_nonzero >= 0 and current_permutation[last_nonzero] == 0:
            last_nonzero -= 1

        # Build up all possible productions of the length given in
        # the permutation.
        G = Grammar(self.table)
        unzipped_permutations = list()
        for i in range(len(current_permutation)):
            symbol = rhs[i + base_index]

            # If the symbol is a terminal, _kfirst won't return anything.
            # (Although, it should really return that symbol, if we have
            # a k of 1.)  So, instead, we get the exact length production,
            # which will return the symbol. (If it's a k of 1.)
            #
            # We should only allow adding a firstset to the result if the
            # result will be used in a _completed_ followset.  Otherwise, the
            # partial firstset could be appended to during the fixpoint
            # solution.
            if i == last_nonzero and not is_term(symbol) and allow_firstset:
                all_exact_length_productions = list()
                for item in self._kfirst(
                    symbol, current_permutation[i], True, debug
                ):
                    for subproduction in item.sequences:
                        if len(subproduction) == 1 and subproduction[0] == "ε":
                            continue
                        all_exact_length_productions.append(subproduction)
            else:
                # Get all productions of the exact length specified.
                # If it's not possible, abort.
                all_exact_length_productions = list(
                    G.get_exact(symbol, current_permutation[i])
                )

            if not all_exact_length_productions:
                return
            unzipped_permutations.append(all_exact_length_productions)

        # Zip up the productions of exact length to form the permutations.
        stack = [(SubProduction([]), 0)]
        while stack:
            curr, i = stack.pop()
            if i == len(unzipped_permutations):
                if debug and self.fo_debug:
                    self.fo_debug.add_result(
                        debug,
                        curr,
                    )
                yield curr
                continue
            for terminal in unzipped_permutations[i]:
                stack.append((curr + terminal, i + 1))

    def _kfollow_permutations(
        self,
        production: Production,
        base_index: int,
        k: int,
        parent_debug: Optional[str] = None,
    ) -> Iterable[FollowSet]:
        debug = None
        if self.fo_debug and parent_debug:
            debug = self.fo_debug.add_call(
                [production, base_index, k], dict(), ["_kfollow_permutations"]
            )
            self.fo_debug.add_child(parent_debug, debug)

        lhs, rhs = production
        # If the symbol occurred at the end of the RHS, then we have
        # no permutations -- it'll be composed of the LHS followset.
        if base_index == len(rhs):
            yield FollowSet.partial([SubProduction([])], lhs, k)
            return

        queue = [(i,) for i in range(k + 1)]  # type: List[Tuple[int, ...]]
        guard = 500
        while queue:
            if not guard:
                raise Exception("Reached max iteration.")
            guard -= 1

            current_permutation = queue.pop()
            if debug and self.fo_debug:
                new_debug = self.fo_debug.add_call(
                    [current_permutation],
                    dict(),
                    ["_kfollow_permutations", f"iteration {500 - guard}"],
                )
                self.fo_debug.add_child(debug, new_debug)
                debug = new_debug
            at_max_k = sum(current_permutation) == k
            assigned_each_symbol_value = (len(rhs) - base_index) <= len(
                current_permutation
            )
            if at_max_k:
                ret = FollowSet.complete(
                    list(
                        self._kfollow_resolve_permutation(
                            production,
                            base_index,
                            current_permutation,
                            allow_firstset=True,
                            parent_debug=debug,
                        )
                    ),
                    lhs,
                    k,
                )
                if debug and self.fo_debug:
                    self.fo_debug.add_result(debug, ret)
                yield ret
            elif assigned_each_symbol_value:
                # This means we have a partial solution, which will need to
                # be resolved during the fixpoint phase.
                ret = FollowSet.partial(
                    list(
                        self._kfollow_resolve_permutation(
                            production,
                            base_index,
                            current_permutation,
                            allow_firstset=False,
                            parent_debug=debug,
                        )
                    ),
                    lhs,
                    k,
                )
                if debug and self.fo_debug:
                    self.fo_debug.add_result(debug, ret)
                yield ret
            else:
                for i in range(k - sum(current_permutation) + 1):
                    queue.append(current_permutation + (i,))

    def _kfollow(
        self, symbol, k: int, parent_debug: Optional[str] = None
    ) -> Iterable[FollowSet]:
        debug = None
        if self.fo_debug and parent_debug:
            debug = self.fo_debug.add_call([symbol, k], dict(), ["_kfollow"])
            self.fo_debug.add_child(parent_debug, debug)

        G = Grammar(self.table)
        for lhs, rhs in G:
            for i in [i for i, s in enumerate(rhs) if s == symbol]:
                yield from self._kfollow_permutations(
                    (lhs, list(rhs)), i + 1, k, parent_debug=debug
                )

    def _kfollow_fixpoint_solution(
        self,
        followset_lookup: Dict[str, List[FollowSet]],
        parent_debug: Optional[str] = None,
    ) -> Dict[str, List[FollowSet]]:
        """Resolve partial followsets into complete followsets.

        Args:
            followset_lookup: A dictionary containing the non-terminal symbols
                and their complete/partial followsets.  Assumes that all
                non-terminal symbols present are keys in this dictionary,
                and that all followsets have the same k value.
            parent_debug: A debugging token.

        Returns:
            The dict of followsets, but completed.

        # noqa: DAR401

        """
        debug = None
        if self.fo_debug and parent_debug:
            debug = self.fo_debug.add_call(
                [followset_lookup], dict(), ["_kfollow_fixpoint_solution"]
            )
            self.fo_debug.add_child(parent_debug, debug)

        iteration_guard = 500
        changed = True
        while changed:
            changed = False

            if not iteration_guard:
                raise Exception(
                    "Reached iteration limit during fixpoint solution."
                )
            iteration_guard -= 1

            for followsets in followset_lookup.values():
                for followset in followsets:
                    followset.changed = False
                    if followset.is_complete:
                        continue
                    for other in followset_lookup[followset.follow]:
                        followset.append(other)
                changed |= any([x.changed for x in followsets])

        if debug and self.fo_debug:
            self.fo_debug.add_result(debug, followset_lookup)

        return followset_lookup

    def kfollow(self, k: int) -> Dict[str, Set[Union[str, Tuple[str, ...]]]]:
        # Track debugging information.
        debug = None
        if self.fo_debug:
            debug = self.fo_debug.add_call([k], dict(), ["kfollow"])

        F = {x: [] for x, _ in self.table}  # type: Dict[str, List[FollowSet]]
        initial_start = FollowSet.complete(
            [SubProduction(["$"])], self.start, 1
        )
        F[self.start].append(initial_start)
        for i in range(1, k + 1):
            for symbol in F.keys():
                for followset in self._kfollow(symbol, i, parent_debug=debug):
                    F[symbol].append(followset)
            F = self._kfollow_fixpoint_solution(F, parent_debug=debug)
        ret = defaultdict(
            lambda: set()
        )  # type: Dict[str, Set[Union[str, Tuple[str, ...]]]]  # noqa: E501
        for symbol, followsets in F.items():
            for follow in {followset.follow for followset in followsets}:
                # TODO: This could probably be improved, performance-wise.
                followset = reduce(
                    FollowSet.upgrade,
                    [
                        followset
                        for followset in followsets
                        if followset.follow == follow
                    ],
                )
                for subproduction in followset.additional | set(
                    followset.completes
                ):
                    if len(subproduction) == 1:
                        item = subproduction[0]
                        assert isinstance(item, str)
                        ret[symbol].add(item)
                    else:
                        ret[symbol].add(tuple(subproduction))

        if debug and self.fo_debug:
            self.fo_debug.add_result(debug, ret)

        return dict(ret)

    def follow(self, first: Dict[str, Set[str]]) -> Dict[str, Set[str]]:
        """Calculate the follow set for generating an LL(1) table.

        Args:
            first: The previously calculated first-set.

        Returns:
            A mapping from non-terminal productions to the first
            terminals which can follows them.

        """
        return {
            key: {x for x in value if isinstance(x, str)}
            for key, value in self.kfollow(1).items()
        }

    def _matches(self, rhs: Tuple[str, ...], terms: Tuple[str, ...]) -> bool:
        queue = deque([(rhs, terms)])
        while queue:
            children, remaining = queue.pop()
            if not (children and remaining):
                continue

            # Consume the terminal symbols.
            i = 0
            skip_this = False
            while (i < len(children)
                   and i < len(remaining)
                   and not is_nonterm(children[i])):
                if children[i] != remaining[i]:
                    skip_this = True
                    break
                i += 1
            if skip_this:
                continue
            if not is_nonterm(children[0]):
                children, remaining = children[i:], remaining[i:]

            if not remaining:
                return True
            if not children:
                continue
            if not is_nonterm(children[0]):
                return False

            head, rest = children[0], children[1:]
            for production in self.adjacency_matrix[head]:
                if is_epsilon(production[0]):
                    queue.appendleft((rest, remaining))
                else:
                    queue.appendleft((tuple(production) + rest, remaining))

    # TODO: Change to be stored as an intermediate result of Fi, Fo
    def get_production_leading_to_terminal(
        self, nonterm: str, term: Union[str, Tuple[str, ...]]
    ) -> Iterable[Production]:
        def has_nonterm(l: List[str], value: Union[str, Tuple[str, ...]]) -> bool:
            if isinstance(value, str):
                return is_nonterm(l[0])
            else:
                return any(map(is_nonterm, l[:len(value)]))

        for rhs in self.adjacency_matrix[nonterm]:
            remaining = term
            if isinstance(term, str):
                remaining = (term,)
            if self._matches(tuple(rhs), remaining):
                yield (nonterm, rhs)


    def generate_ktable(
        self,
        first: Dict[str, Set[Union[str, Tuple[str, ...]]]],
        follow: Dict[str, Set[Union[str, Tuple[str, ...]]]],
        k: int,
    ) -> Dict[str, Dict[Union[str, Tuple[str, ...]], Production]]:
        table = {
            nonterm: dict() for nonterm in self.nonterminals
        }  # type: Dict[str, Dict[Union[str, Tuple[str, ...]]]]
        for nonterm, terms in first.items():
            for term in terms:
                productions = list(
                    self.get_production_leading_to_terminal(nonterm, term)
                )
                assert len(productions) == 1, (
                    f"Ambiguous grammar.  Firstset contains multiple productions"
                    f" for {nonterm} when encountering {term}: {productions}."
                )
                production = productions[0]
                if term == "ε":
                    for term2 in follow[nonterm]:
                        if term2 not in table[nonterm]:
                            table[nonterm][term2] = production
                else:
                    table[nonterm][term] = production
        return table

    def generate_table(
        self, first: Dict[str, Set[str]], follow: Dict[str, Set[str]]
    ) -> Dict[str, Dict[str, Production]]:
        return self.generate_ktable(first, follow, 1)


def normalize_for_table(symbol: Union[str, Tuple[str, ...]]) -> str:
    if (
        isinstance(symbol, str)
        and symbol.startswith('"')
        and symbol.endswith('"')
    ):
        return symbol[1:-1]
    elif isinstance(symbol, str):
        return repr(symbol)
    elif isinstance(symbol, tuple):
        return '(' + ', '.join(map(normalize_for_table, symbol)) + ')'


PARSE_EXCEPTION = r"""
class ParseException(Exception):
    pass
"""

PARSE = r"""
    def _fill_buff(self, token_buff):
        # type: (Tuple[BuffToken, ...]) -> Tuple[BuffToken, ...]
        new_buff = token_buff
        while len(new_buff) < self.k:
            try:
                new_buff += (next(self.tokens),)
            except StopIteration:
                if None not in token_buff:
                    new_buff += (None,)
                break
        return new_buff

    def _behead(self, buff):
        # type: (Tuple[T, ...]) -> Tuple[Optional[T], Tuple[T, ...]]
        head = None
        if buff:
            head = buff[0]
        return head, tuple([x for x in buff[1:]])

    def _get_token_type_buff(self, token_buff):
        # type: (Tuple[BuffToken, ...]) -> Tuple[BuffTokenType, ...]
        return tuple([x.token_type if x else "$" for x in token_buff])

    def parse(self):
        # type: () -> Node
        root = Node(node_type={start_symbol})
        stack = deque([root])
        token_buff = self._fill_buff(tuple())
        token_type_buff = self._get_token_type_buff(token_buff)
        while stack:
            curr = stack.popleft()
            if curr.node_type == "ε":
                continue

            # We're at a terminal node; we should be able to consume
            # from the token stream.
            if isinstance(curr.node_type, TokenType):
                if (
                    len(token_type_buff) > 0
                    and curr.node_type == token_type_buff[0]
                ):
                    curr.value, token_buff = self._behead(token_buff)
                    token_buff = self._fill_buff(token_buff)
                    token_type_buff = self._get_token_type_buff(token_buff)
                    continue
                else:
                    raise ParseException(
                        "Expected token type {{}}, but got {{}}".format(
                            token_type_buff, curr.node_type
                        )
                    )
            if curr.node_type not in self.table:
                raise ParseException(
                    "Expected {{}} to be in grammar, but was not.".format(
                        curr,
                    )
                )

            # Drop off the end of the token lookahead to see if a shorter
            # version is in the table.
            index = tuple(token_type_buff)
            if len(index) == 1:
                index = index[0]
            while index and index not in self.table[curr.node_type]:
                if isinstance(index, str):
                    index = None
                    break
                index = index[:-1]
                if len(index) == 1:
                    index = index[0]
            if not index:
                raise ParseException(
                    "Expected {{}} to be in a production "
                    "of {{}}, but was not.".format(token_buff, curr)
                )
            lhs, rhs = self.table[curr.node_type][index]

            # `extendleft` appends in reverse order,
            # so we have to reverse before extending.
            # Otherwise, right-recursive productions will
            # never finish parsing.
            children = [Node(x) for x in rhs]  # type: ignore
            curr.children = children
            stack.extendleft(children[::-1])
        return root
"""


def generate_parser(grammar: str,
                    imports: Optional[str],
                    k: int = 1) -> str:
    generator = LLTableGenerator(grammar)
    first = generator.kfirst(k)
    follow = generator.kfollow(k)
    table = generator.generate_ktable(first, follow, k)
    if imports:
        imports_or_blank = f"\n{imports}\n"
    else:
        imports_or_blank = ""

    parser = [
        f"# Generated on {datetime.datetime.now()}",
        "",
        "from collections import deque",
        "from typing import Dict, List, Iterator, Optional, Tuple, TypeVar, Union",  # noqa: E501
        imports_or_blank,
        PARSE_EXCEPTION,
        "",
        "",
        "T = TypeVar(\"T\")",
        "",
        "",
        "# Types which allow for sentinal values None and \"$\"",
        "BuffToken = Optional[Token]",
        "BuffTokenType = Union[TokenType, str]",
        "class Parser(object):",
        "    table = {",
    ]
    for row_value, row in table.items():
        parser.append(" " * 8 + f"{normalize_for_table(row_value)}: {{")
        for col_value, production in row.items():
            parser.append(" " * 12 + f"{normalize_for_table(col_value)}: (")
            lhs, rhs = production
            parser.append(" " * 16 + f"{normalize_for_table(lhs)},")
            parser.append(" " * 16 + "[")
            for value in rhs:
                parser.append(" " * 20 + f"{normalize_for_table(value)},")
            parser.append(" " * 16 + "]")
            parser.append(" " * 12 + "),")
        parser.append(" " * 8 + "},")
    parser.append(" " * 4 + "}")

    parser.append("")
    parser.append("    def __init__(self, tokens):")
    parser.append("        # type: (Iterator[Token]) -> None")
    parser.append("        self.tokens = tokens")
    parser.append(f"        self.k = {k}")
    parser.append("")
    parser.append(PARSE.strip("\n").format(start_symbol=repr(generator.start)))

    return "\n".join(parser)