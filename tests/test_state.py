from app.conversation.state import ConversationState


def test_states_include_barge_in():
    assert ConversationState.INTERRUPTED.value == "INTERRUPTED"
