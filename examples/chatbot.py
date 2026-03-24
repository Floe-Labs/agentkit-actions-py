"""LangChain chatbot example with Floe actions.

Run: PRIVATE_KEY=0x... OPENAI_API_KEY=sk-... python examples/chatbot.py
Requires: pip install floe-agentkit-actions[langchain] langchain-openai langgraph
"""

from __future__ import annotations

import os


def main() -> None:
    from coinbase_agentkit.wallet_providers import EvmWalletProvider
    from langchain_openai import ChatOpenAI
    from langgraph.prebuilt import create_react_agent

    from floe_agentkit_actions.integrations.langchain import get_floe_langchain_tools

    # Create wallet
    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        print("Error: Set PRIVATE_KEY environment variable")
        return

    wallet_provider = EvmWalletProvider(
        private_key=private_key,
        network_id="base-mainnet",
    )

    # Get Floe tools as LangChain tools
    tools = get_floe_langchain_tools(wallet_provider)
    print(f"Loaded {len(tools)} Floe tools\n")

    # Create LangChain agent
    llm = ChatOpenAI(model="gpt-4o")
    agent = create_react_agent(llm, tools)

    # Chat loop
    print("Floe DeFi Agent (type 'exit' to quit)\n")
    while True:
        user_input = input("You: ").strip()
        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            break

        response = agent.invoke({"messages": [("user", user_input)]})
        last_msg = response["messages"][-1]
        print(f"\nAssistant: {last_msg.content}\n")


if __name__ == "__main__":
    main()
