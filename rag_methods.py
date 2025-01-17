import os
import dotenv
from time import time
import streamlit as st

from langchain_community.document_loaders.text import TextLoader
from langchain_community.document_loaders import (
    WebBaseLoader,
    PyPDFLoader,
    Docx2txtLoader,
)
from langchain_community.vectorstores import Chroma
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.chat_models import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain.chains import create_history_aware_retriever, create_retrieval_chain
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.messages import HumanMessage, AIMessage


dotenv.load_dotenv()

# --- Constants ---
os.environ["USER_AGENT"] = "myagent"
DB_DOCS_LIMIT = 10

# Hardcoded OpenAI API Key
OPENAI_API_KEY = st.secrets["openai"]["api_key"]

# --- Functions ---

def load_doc_to_db():
    """Load documents into ChromaDB."""
    if "rag_docs" in st.session_state and st.session_state.rag_docs:
        docs = []
        for doc_file in st.session_state.rag_docs:
            if doc_file.name not in st.session_state.rag_sources:
                if len(st.session_state.rag_sources) < DB_DOCS_LIMIT:
                    os.makedirs("source_files", exist_ok=True)
                    file_path = f"./source_files/{doc_file.name}"
                    with open(file_path, "wb") as file:
                        file.write(doc_file.read())

                    try:
                        if doc_file.type == "application/pdf":
                            loader = PyPDFLoader(file_path)
                        elif doc_file.name.endswith(".docx"):
                            loader = Docx2txtLoader(file_path)
                        elif doc_file.type in ["text/plain", "text/markdown"]:
                            loader = TextLoader(file_path)
                        else:
                            st.warning(f"Document type {doc_file.type} not supported.")
                            continue

                        docs.extend(loader.load())
                        st.session_state.rag_sources.append(doc_file.name)

                    except Exception as e:
                        st.toast(f"Error loading document {doc_file.name}: {e}", icon="⚠️")
                    finally:
                        os.remove(file_path)
                else:
                    st.error(f"Maximum number of documents reached ({DB_DOCS_LIMIT}).")

        if docs:
            _split_and_load_docs(docs)
            st.toast("Documents loaded successfully.", icon="✅")


def load_url_to_db():
    """Load URLs into ChromaDB."""
    if "rag_url" in st.session_state and st.session_state.rag_url:
        url = st.session_state.rag_url
        docs = []
        if url not in st.session_state.rag_sources:
            if len(st.session_state.rag_sources) < DB_DOCS_LIMIT:
                try:
                    loader = WebBaseLoader(url)
                    docs.extend(loader.load())
                    st.session_state.rag_sources.append(url)

                except Exception as e:
                    st.error(f"Error loading document from {url}: {e}")

                if docs:
                    _split_and_load_docs(docs)
                    st.toast(f"Document from URL *{url}* loaded successfully.", icon="✅")
            else:
                st.error("Maximum number of documents reached.")


def stream_llm_response(llm, messages):
    """Stream clean responses from the LLM."""
    response_message = ""
    for chunk in llm.stream(messages):
        if hasattr(chunk, "content"):  # Check if chunk has 'content' attribute
            response_message += chunk.content
            yield chunk.content
        elif isinstance(chunk, str):  # Handle plain strings
            response_message += chunk
            yield chunk
        else:  # Handle unknown types
            response_message += str(chunk)
            yield str(chunk)
    st.session_state.messages.append({"role": "assistant", "content": response_message})

from langchain_core.messages import HumanMessage, AIMessage

def stream_llm_rag_response(llm_stream, messages):
    """Stream only the assistant's clean response from the RAG chain."""
    # Get the RAG chain
    conversation_rag_chain = get_conversational_rag_chain(llm_stream)
    response_message = ""

    # Extract user input (last message) and format chat history
    user_input = messages[-1].content if hasattr(messages[-1], "content") else str(messages[-1])
    chat_history = [
        {"role": "user" if isinstance(m, HumanMessage) else "assistant", "content": m.content}
        for m in messages[:-1]
    ]

    # Run the RAG chain with user input and chat history
    for chunk in conversation_rag_chain.stream({
        "input": user_input,       # Current user input
        "messages": chat_history,  # Chat history for context
        "context": "",             # Placeholder for context
    }):
        # Extract clean assistant response
        if isinstance(chunk, dict):
            content = chunk.get("answer") or chunk.get("content", "")
        elif hasattr(chunk, "content"):
            content = chunk.content
        elif isinstance(chunk, str):
            content = chunk
        else:
            content = str(chunk)

        # Stream and append only the assistant's response
        if content.strip():  # Skip empty chunks
            response_message += content
            yield content

    # Append the clean response to session state
    st.session_state.messages.append({"role": "assistant", "content": response_message})

from chromadb import Client
from chromadb.config import Settings
from langchain.embeddings.openai import OpenAIEmbeddings
from time import time
import uuid

def initialize_vector_db(docs):
    """Initialize ChromaDB with OpenAI Embeddings and a persistent configuration."""
    settings = Settings(
        chroma_db_impl="duckdb+parquet",  # Use DuckDB+Parquet backend
        persist_directory="./chroma_db",  # Directory for database files
    )

    # Initialize the Chroma client with persistent settings
    client = Client(settings)

    # Define a unique collection name
    collection_name = f"collection_{str(time()).replace('.', '')[:14]}"

    # Create or get the collection
    collection = client.get_or_create_collection(name=collection_name)

    # Initialize OpenAI embeddings
    embedding = OpenAIEmbeddings(api_key=OPENAI_API_KEY)

    # Add documents to the collection
    for doc in docs:
        doc_id = str(uuid.uuid4())  # Generate a unique ID for each document
        collection.add(
            ids=[doc_id],
            documents=[doc.page_content],
            metadatas=[doc.metadata],
            embeddings=[embedding.embed_query(doc.page_content)],
        )

    return collection


def _split_and_load_docs(docs):
    """Split and load documents into the vector database."""
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=5000,
        chunk_overlap=1000,
    )
    document_chunks = text_splitter.split_documents(docs)

    if "vector_db" not in st.session_state:
        st.session_state.vector_db = initialize_vector_db(document_chunks)
    else:
        st.session_state.vector_db.add_documents(document_chunks)


def get_conversational_rag_chain(llm):
    """Create conversational RAG chain."""
    retriever = st.session_state.vector_db.as_retriever()
    retriever_chain = create_history_aware_retriever(
        llm,
        retriever,
        ChatPromptTemplate.from_messages([
            MessagesPlaceholder(variable_name="messages"),
            ("user", "{input}"),
        ]),
    )
    prompt = ChatPromptTemplate.from_messages([
        ("system", "You are a helpful assistant. Use the context to answer the user's queries.\n{context}"),
        MessagesPlaceholder(variable_name="messages"),
        ("user", "{input}"),
    ])
    stuff_documents_chain = create_stuff_documents_chain(llm, prompt)
    return create_retrieval_chain(retriever_chain, stuff_documents_chain)
