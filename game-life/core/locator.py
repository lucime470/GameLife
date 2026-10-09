import winreg
from pathlib import Path


def find_steam_path()->str:
    try:
        #打开注册表中的steam的位置
        key=winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Valve\Steam"
        )
        #读取具体的路径值
        path,_ = winreg.QueryValueEx(key, "InstallPath")
        winreg.CloseKey(key)
        if Path(path).exists():
            return str(Path(path))

    except(FileNotFoundError,OSError):
        pass

    raise FileNotFoundError("未找到 Steam 安装目录，请手动输入路径")

def list_user_accounts(steam_path:str)->list[dict]:
    #拼接userdata路径并扫描
    userdata=Path(steam_path)/"userdata"
    if not userdata.exists():
        raise FileNotFoundError(f"未找到 userdata 目录: {userdata}")

    accounts=[]
    #遍历目录下的子项并检查是否是文件夹
    for folder in userdata.iterdir():
        if not folder.is_dir():
            continue
        account_id=folder.name
        localconfig=folder/"config"/"localconfig.vdf"
        user_name=""
        if localconfig.exists():
            try:
                import vdf
                data=vdf.load(open(localconfig,"r",encoding="utf-8-sig"))
                friends=data.get("UserLocalConfigStore",{}).get("friends",{})
                user_name=friends.get("PersonaName","")
            except Exception:
                pass

        accounts.append({
            "id":account_id,
            "name":user_name or account_id,
        })
        
    return accounts

def select_account(accounts:list[dict])->dict:
    #检查账号个数
    if len(accounts)==0:
        raise ValueError("未找到任何 Steam 账号")
    if len(accounts)==1:
        print(f"自动选择账号: {accounts[0]['name']} ({accounts[0]['id']})")
        return accounts[0]

    print("找到多个 Steam 账号，请选择：")
    for i, acc in enumerate(accounts):
        print(f"  [{i}] {acc['name']} ({acc['id']})")

    while True:
        try:
            choice=int(input("请输入序号: "))
            if 0<=choice<len(accounts):
                return accounts[choice]
        except EOFError as exc:
            # 非交互环境（无 stdin / 输入被重定向到空管道）下 input() 会抛
            # EOFError；转成带指引的 ValueError 交给调用方友好提示，
            # 不再向外冒裸堆栈。
            raise ValueError(
                "未在交互终端中运行，无法读取账号选择输入；"
                "请改用命令行的 --account-id <ID> 指定账号"
            ) from exc
        except ValueError:
            pass
        print("输入无效，请重新输入")