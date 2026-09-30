#网页上传服务
#启动服务streamlit run your_script.pys

import time

import streamlit as st  #简单的前端框架
from knowledge_base import KnowledgeBaseService


st.title("知识库服务更新")

uploader_file = st.file_uploader(
    "请上传TXT文件",
    type=['txt'],
    accept_multiple_files=False,#拒绝文件批量上传
)

if "service" not in st.session_state:
    st.session_state["service"] = KnowledgeBaseService()

if uploader_file is not None:
    file_name = uploader_file.name
    file_type = uploader_file.type
    file_size = uploader_file.size/1024

    st.subheader(f"文件名:{file_name}")
    st.write(f"格式：{file_type} | 大小:{file_size:.2f}KB")


    text = uploader_file.getvalue().decode("utf-8")
    with st.spinner("知识库记载中。。。"):
        time.sleep(1)
        result = st.session_state["service"].upload_by_str(text, file_name)
        st.write(result)



