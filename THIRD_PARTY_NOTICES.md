# 외부 구성 요소

이 프로젝트는 별도 프로세스로 아래 도구를 실행합니다. 다운로드 실행 파일과 Python 환경은 사용자 컴퓨터에 설치되며 소스 저장소에는 포함하지 않습니다.

| 구성 요소 | 설치 출처 | 라이선스/고지 |
|---|---|---|
| yt-dlp 2026.08.19 (Python wheel) | https://pypi.org/project/yt-dlp/ | [Unlicense](https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE). 배포 종류에 따라 포함 의존성 라이선스가 다르므로 Windows 단독 exe와 wheel을 혼동하지 않음 |
| yt-dlp-ejs 0.8.0 | PyPI | Unlicense 및 포함 JavaScript 구성 요소의 MIT/ISC 고지 확인 |
| FFmpeg / ffprobe N-127203-ga35c879992-20261004 | https://github.com/yt-dlp/FFmpeg-Builds/releases | 실제 빌드에 `--enable-gpl --enable-version3`. GPL v3 이상. 압축 파일의 `LICENSE.txt` 및 `tools/help/ffmpeg-license.txt` 보존 |
| Node.js 24.12.0 | 이 컴퓨터에 설치된 Node.js | MIT 및 Node.js에 포함된 외부 구성 요소 고지. 이 프로젝트가 재배포하지 않음 |
| FastAPI, Uvicorn, Starlette, Pydantic | PyPI | 각 패키지의 MIT/BSD 라이선스 및 배포 메타데이터 참조 |
| yt-dlp 기본 의존성 | PyPI, `requirements-lock.txt` | 예: mutagen GPL v2 이상, certifi MPL 2.0, requests Apache 2.0. `.venv/Lib/site-packages/*.dist-info`의 라이선스 메타데이터 보존 |

FFmpeg 빌드 원본 압축 파일과 동봉 라이선스는 `tools/`에 남아 있습니다. 설치본에 대한 SHA256·빌드 옵션·다운로드 URL은 `tools/provenance.json`과 `docs/toolchain.json`에 기록합니다. 설치는 수정하지 않은 upstream 빌드를 사용합니다.

참조:

- https://ffmpeg.org/legal.html
- https://github.com/yt-dlp/FFmpeg-Builds
- https://github.com/yt-dlp/yt-dlp#licensing
- https://github.com/yt-dlp/yt-dlp/blob/master/THIRD_PARTY_LICENSES.txt
- https://github.com/yt-dlp/ejs

실행 파일을 포함한 배포용 패키지를 별도로 만들지 않았습니다. 소스 코드만 공유할 때 `tools/`, `.venv/`, `data/`, `downloads/`, `config.json`, `test-results/`를 포함하지 마세요. GPL 실행 파일을 별도로 재배포할 경우 정확한 대응 소스와 빌드 구성, 라이선스 고지 등 해당 라이선스 요건을 충족해야 하며, upstream URL만 적는 것으로 모든 재배포 요건을 충족한다고 가정하면 안 됩니다.

테스트 영상·사인파·자막은 이 프로젝트에서 직접 생성/작성합니다. 제3자 영상이나 썸네일은 소스에 포함하지 않습니다.
