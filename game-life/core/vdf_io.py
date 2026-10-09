import vdf
from pathlib import Path

def read_playtime_data(steam_path:str,account_id:str)->dict:
    localconfig_path=Path(steam_path)/"userdata"/account_id/"config"/"localconfig.vdf"
    if not localconfig_path.exists():
        raise FileNotFoundError(f"未找到 localconfig.vdf: {localconfig_path}")

    try:
        with open(localconfig_path,"r",encoding="utf-8-sig") as f:
            #打开文件解析
            data=vdf.load(f)
    except Exception as e:
        raise RuntimeError(f"解析 localconfig.vdf 失败: {e}")

    steam=(data.get("UserLocalConfigStore",{})
           .get("Software",{})
           .get("Valve",{})
           .get("Steam",{}))

    apps=steam.get("Apps",{})

    result={}
    #把vdf文件中的键值对全部打包成元组
    for appid_str,info in apps.items():
        if not appid_str.isdigit():
            continue
        result[int(appid_str)] = {
            "playtime":int(info.get("Playtime",0)),
            "last_played":int(info.get("LastPlayed",0))
        }
    return result

def read_existing_tags(steam_path:str,account_id:str)->dict:
    #读取现有分类
    shared_path=Path(steam_path)/"userdata"/account_id/"7"/"remote"/"sharedconfig.vdf"
    if not shared_path.exists():
        return {}

    try:
        with open(shared_path,"r",encoding="utf-8-sig") as f:
            data=vdf.load(f)
    except Exception as e:
        raise RuntimeError(f"解析 sharedconfig.vdf 失败: {e}")

    steam = (data.get("UserRoamingConfigStore", {})
             .get("Software", {})
             .get("Valve", {})
             .get("Steam", {}))

    apps = steam.get("Apps", {})

    result={}
    #提取分类
    for appid_str,info in apps.items():
        if not appid_str.isdigit():
            continue
        tags=info.get("tags",{})
        tag_set=set()
        for tag in tags.values():
            tag_set.add(tag)
        result[int(appid_str)] = tag_set
    return result

def build_sharedconfig(data:dict,games_tags:dict)->str:
    #生成新的sharedconfig文本（合并模式：保留原 appid 的其他子键，仅更新/追加 tags）
    steam=(data.get("UserRoamingConfigStore",{})
           .get("Software",{})
           .get("Valve",{})
           .get("Steam",{}))

    apps=steam.setdefault("Apps",{})

    for appid,tags in games_tags.items():
        entry=apps.get(str(appid))
        if not isinstance(entry,dict):
            entry=apps[str(appid)]={}
        # 排序后编号，保证输出稳定（tags 可能来自 set），多次调用结果一致
        entry["tags"]={str(i): t for i,t in enumerate(sorted(tags))}

    return vdf.dumps(data)

def save_sharedconfig(steam_path:str,account_id:str,content:str)->None:
    #写回
    shared_path=Path(steam_path)/"userdata"/account_id/"7"/"remote"/"sharedconfig.vdf"
    if not shared_path.exists():
        raise FileNotFoundError(f"未找到 sharedconfig.vdf: {shared_path}")
    backup_path=shared_path.with_suffix(".bak")
    # 备份
    import shutil
    shutil.copy2(shared_path,backup_path)
    with open(shared_path,"w",encoding="utf-8-sig") as f:
        f.write(content)
