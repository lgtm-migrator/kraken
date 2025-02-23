def stage(ctx):
    return {
        "parent": "Build",
        "triggers": {
            "manual": True,
        },
        "parameters": [],
        "configs": [],
        "jobs": [{
            "name": "publish",
            "timeout": 1200,
            "steps": [{
                "tool": "artifacts",
                "action": "download",
                "source": "kraken.tar.gz"
            }, {
                "tool": "shell",
                "cmd": "tar -zxf kraken.tar.gz",
            }, {
                "tool": "artifacts",
                "action": "download",
                "public": True,
                "source": [
                    "kraken-docker-compose-0.#{KK_FLOW_SEQ}.yaml",
                    "server/dist/krakenci_server-0.#{KK_FLOW_SEQ}.tar.gz",
                    "agent/krakenci_agent-0.#{KK_FLOW_SEQ}.tar.gz",
                    "client/dist/krakenci_client-0.#{KK_FLOW_SEQ}.tar.gz",
                    "ui/dist/krakenci_ui-0.#{KK_FLOW_SEQ}.tar.gz",
                ],
                "cwd": "kraken"
            }, {
                "tool": "shell",
                "script": """
                    rake -t prepare_env
                    rake -t publish_client publish_server
                """,
                "cwd": "kraken",
                "timeout": 300,
                "env": {
                    "kk_ver": "0.#{KK_FLOW_SEQ}",
                    "PYPI_PASSWORD": "#{KK_SECRET_SIMPLE_pypi_password}"
                }
            }, {
                "tool": "shell",
                "cmd": "git config --global user.email 'godfryd@gmail.com'; git config --global user.name 'Michal Nowikowski'"
            }, {
                "tool": "git",
                "checkout": "git@github.com:Kraken-CI/helm-repo.git",
                "access-token": "github_token",
                "branch": "gh-pages"
            }, {
                "tool": "shell",
                "script": """
                   curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo apt-key add -
                   echo 'deb https://packages.cloud.google.com/apt cloud-sdk main' | sudo tee -a /etc/apt/sources.list.d/google-cloud-sdk.list
                   sudo apt update
                   sudo apt install -y --no-install-recommends google-cloud-sdk
                """,
                "timeout": 300
            }, {
                "tool": "shell",
                "cmd": "echo \"${GOOGLE_KEY}\" | base64 -d > /tmp/key.json",
                "env": {
                    "GOOGLE_KEY": "#{KK_SECRET_SIMPLE_google_key}"
                }
            }, {
                "tool": "shell",
                "cmd": "gcloud auth activate-service-account lab-kraken-ci@kraken-261806.iam.gserviceaccount.com --project=kraken-261806 --key-file=/tmp/key.json"
            }, {
                "tool": "shell",
                "cmd": "rake kk_ver=0.#{KK_FLOW_SEQ} mark_images_as_published",
                "cwd": "kraken",
                "timeout": 120
            }, {
                "tool": "shell",
                "cmd": "rake kk_ver=0.#{KK_FLOW_SEQ} helm_dest=../helm-repo/charts helm_release",
                "cwd": "kraken",
                "timeout": 120
            }, {
                "tool": "shell",
                "cmd": "rake kk_ver=0.#{KK_FLOW_SEQ} github_release",
                "cwd": "kraken",
                "env": {
                    "GITHUB_TOKEN": "#{KK_SECRET_SIMPLE_github_token}"
                },
                "timeout": 300
            }],
            "environments": [{
                "system": "krakenci/bld-kraken:20221106",
                "executor": "docker",
            	"agents_group": "all",
                "config": "default"
            }]
        }]
    }
