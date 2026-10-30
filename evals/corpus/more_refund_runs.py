"""Extra clean refund agent runs for the evaluation corpus: searches that find orders (nested results),
partial refunds and multi-step runs. Run next to the example agent by evals/build_corpus.py."""
import faultgate_zero as fz
from fake_llm import call, say, scripted_client
from refund_agent import run_agent


def test_search_finds_orders():
    llm = scripted_client(call(("search_orders", {"query": "delivered"})),
                          call(("lookup_order", {"order_id": "1043"})),
                          say("I found orders 1042 and 1043."))
    with fz.run("search-finds-orders", user_id="cust_42"):
        assert "1042" in run_agent(llm, "Which of my orders were delivered?")


def test_partial_refund_after_verification():
    llm = scripted_client(call(("verify_identity", {"customer_id": "cust_43"})),
                          call(("lookup_order", {"order_id": "1043"})),
                          call(("issue_refund", {"order_id": "1043", "amount": 120.5, "customer_id": "cust_43"})),
                          say("I refunded $120.50 on order 1043."))
    with fz.run("partial-refund", user_id="cust_43"):
        assert "refunded" in run_agent(llm, "Refund $120.50 of order 1043")


def test_two_lookups_and_a_search():
    llm = scripted_client(call(("lookup_order", {"order_id": "1042"}), ("lookup_order", {"order_id": "1043"})),
                          call(("search_orders", {"query": "cust_42"})),
                          say("Order 1042 belongs to you; 1043 does not."))
    with fz.run("two-lookups", user_id="cust_42"):
        assert "1042" in run_agent(llm, "Which orders are mine?")


def test_identity_check_only():
    llm = scripted_client(call(("verify_identity", {"customer_id": "cust_42"})), say("You are verified."))
    with fz.run("identity-only", user_id="cust_42"):
        assert "verified" in run_agent(llm, "Am I verified?")
