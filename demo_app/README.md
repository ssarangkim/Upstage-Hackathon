# 중고차 기록부 구매판정 데모 (로컬)

성능·상태점검기록부 PDF를 올리면 Upstage Studio 워크플로우를 Agents API로 실행하고 결과를 보여줌.
페이지 탭: 데모(기록된 10건), 검증, 직접 실행(PDF 업로드 또는 샘플 실행).

## 설치

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

macOS/Linux는 `source .venv/bin/activate`.

## 환경 변수

`.env.example`을 `.env`로 복사하고 `UPSTAGE_API_KEY`에 키를 넣음. `AGENT_ID`, `CONFIG_ID`는 예시 값이 들어 있음.

## 실행

```
python app.py
```

브라우저에서 http://127.0.0.1:5000 을 열고 "직접 실행" 탭을 선택함. 서버는 127.0.0.1에만 바인딩됨.

## 참고

- 실행할 때마다 Studio 사용량이 소모됨.
- API 키는 서버에서만 사용함. `.env`를 커밋하거나 공유하지 말 것 (`.gitignore`에 포함됨).
- 완료된 원본 응답은 `results/`에 저장됨.
- 페이지를 고치려면 `page_tpl.html`을 수정하고 `python build_page.py`로 `static/index.html`을 다시 만듦.

## Render 배포

1. 이 폴더는 저장소의 `demo_app/` 아래에 있고, Blueprint 파일 `render.yaml`은 저장소 루트에 있음 (`.env`, `.venv/`, `results/`는 `demo_app/.gitignore`로 제외됨).
2. Render에서 New → Blueprint를 고르고 저장소를 연결함. 또는 `https://render.com/deploy?repo=<저장소 URL>` 링크를 사용함.
3. 배포 설정 화면에서 `UPSTAGE_API_KEY` 값을 입력함. `AGENT_ID`, `CONFIG_ID`, `PYTHON_VERSION`은 저장소 루트의 `render.yaml`(Blueprint 파일, `rootDir: demo_app`)에 들어 있음.
4. 무료(free) 플랜은 한동안 접속이 없으면 잠들고, 다음 접속 때 깨어나는 데 시간이 걸림.
5. 실행할 때마다 Studio 사용량이 소모됨. 업로드는 25MB 이하 PDF만 가능함.
6. 워커 1개로 돌리므로 작업 정보(메모리)는 재시작하면 사라짐.
