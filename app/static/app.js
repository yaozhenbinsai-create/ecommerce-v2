const pages={
 businesses:{title:'业务配置',subtitle:'沉淀品牌、平台与商品运营规则，让每次内容生产保持一致。',singular:'业务',columns:['业务主体','平台与品类','目标客群 / 价格','状态'],fields:[
  ['name','业务名称','text',true],['brand','品牌 / 店铺','text'],['platforms','运营平台','platformmulti'],['content_language','','hidden'],['category_paths','业务分类参考（平台映射在“分类维护”中配置）','categorymulti'],['amazon_product_type','','hidden'],['variation_theme','变体主题（如 Size / Color / SizeColor）','text'],['set_type','组合规格','select',['单件','两件套','三件套','四件套','多件套']],['piece_count','商品件数','number'],['components','组合明细（用逗号分隔）','stringlist'],['specifications','商品规格参数（全品类通用）','specmanager'],['market_sizes','','hiddenlist'],['cost_price','成本价（元）','number'],['sale_price','销售价（元）','number'],['enabled','启用此业务','checkbox'] ]},
 models:{title:'模型管理',subtitle:'配置文案与图片任务使用的 AI 模型，检测后自动登记整组能力。',singular:'模型',columns:['模型','服务商 / 标识','能力登记','检测状态'],fields:[
  ['name','显示名称','text',true],['provider','服务商','select',['OpenAI 官方账号（Pro）','OpenAI Compatible','OpenAI API','Anthropic','Google','阿里云','火山引擎','自定义']],['capability','主要能力','select',['文本','图片']],['protocol','接口格式','selectValue',[['codex','ChatGPT / Codex 官方账号'],['openai','OpenAI Chat Completions'],['responses','OpenAI Responses'],['images','OpenAI Images'],['anthropic','Claude Messages']]],['model_identifier','模型标识','text',true],['base_url','接口地址','text',true],['api_key','API Key（保存在本机，不回显）','password'],['api_key_env','专用密钥变量（可选，以 ECOMMERCE2_MODEL_KEY_ 开头）','text'],['image_generation_endpoint','图片生成 API 路径（如 /images/generations）','text'],['image_edit_endpoint','图片编辑 API 路径（如 /images/edits）','text'],['enabled','启用此模型','checkbox'] ]},
 plans:{title:'方案配置',subtitle:'把多个模型按业务用途组合成稳定、可复用的调用方案。',singular:'方案',columns:['方案','类型','模型顺序','状态'],fields:[
  ['name','方案名称','text',true],['plan_type','方案类型','select',['商品文案','图片生成','图片精修','竞品分析','客服回复','通用']],['model_ids','使用模型（可多选、可排序）','multiselect'],['description','方案说明','textarea'],['enabled','启用此方案','checkbox'] ]},
 agents:{title:'Agent 管理',subtitle:'为家纺运营岗位配置职责、方案和对应的业务范围。',singular:'Agent',columns:['Agent','岗位 / 职责','执行方案','所属业务','状态'],fields:[
  ['name','Agent 名称','text',true],['role','岗位角色','text'],['responsibilities','职责与工作要求','textarea'],['plan_ids','执行方案（可多选）','planmulti'],['business_id','所属业务','business'],['enabled','启用此 Agent','checkbox'] ]},
 platform_accounts:{title:'平台同步',subtitle:'按已开通的平台账号与站点同步类目、属性和规则；每天北京时间 00:01 自动执行。',singular:'平台账号',columns:['账号','平台 / 站点','授权状态','最近同步'],fields:[
  ['account_name','账号名称','text',true],['platform','平台','select',['亚马逊','淘宝','天猫','京东','抖音','拼多多','1688','Shopee','Lazada','Temu','独立站']],['seller_id','商家 / Seller ID','text'],['enabled','启用此账号','checkbox'] ]},
 supplier_imports:{title:'智能导入',subtitle:'保存供应商原始文件，解析工作表和图片，再由 AI 识别不同格式的商品字段。',singular:'导入批次',columns:['供应商 / 文件','结构读取','AI识别','处理状态'],fields:[]},
 design_projects:{title:'设计工作台',subtitle:'读取资料与原图，自动生成母稿、方案和成品。',singular:'设计项目',columns:['产品 / 供应商','资料来源','商品识别','设计状态'],fields:[
  ['product_name','产品名称','text',true],['supplier_name','供应商名称','text'],['source_file','来源 Excel / 批次','text'],['platform','目标平台','select',['亚马逊','淘宝','天猫','京东','抖音','拼多多','1688','Shopee','Lazada','Temu','独立站']],['business_id','所属业务（可选）','business'],['product_type','产品分类（可选）','text'],['design_brief','商品参数与要求','textarea'],['source_images','原始产品图（每行或逗号分隔）','stringlist'],['enabled','启用此设计项目','checkbox'] ]}
};
const englishDefaultPlatforms=new Set(['亚马逊','Shopee','Lazada','Temu']);
const categoryTreeZh={
'服饰鞋包':{'女装':['连衣裙','衬衫','针织衫','外套','裤装','半身裙'],'男装':['T恤','衬衫','夹克','西装','裤装'],'内衣':['文胸','内裤','家居服','袜子'],'鞋靴':['女鞋','男鞋','童鞋','运动鞋'],'箱包':['女包','男包','旅行箱','功能箱包'],'服饰配件':['帽子','围巾','腰带','手套']},
'美妆个护':{'护肤':['面部护理','面膜','防晒','眼部护理'],'彩妆':['底妆','口红','眼妆','美妆工具'],'洗发护发':['洗发水','护发素','造型产品'],'身体护理':['沐浴用品','身体乳','手足护理'],'香水香氛':['女士香水','男士香水','家居香氛'],'口腔护理':['牙膏','牙刷','漱口水']},
'食品生鲜':{'休闲食品':['坚果炒货','糖果巧克力','饼干糕点','肉干卤味'],'粮油调味':['大米杂粮','食用油','调味品','方便速食'],'饮料冲调':['茶叶','咖啡','乳饮料','饮用水'],'生鲜':['水果','蔬菜','肉禽蛋','水产海鲜'],'酒类':['白酒','葡萄酒','啤酒','洋酒'],'营养保健':['维生素','膳食补充剂','滋补食品']},
'家居家装':{'家具':['卧室家具','客厅家具','餐厅家具','办公家具'],'家装建材':['灯具照明','厨房卫浴','五金工具','墙地面材料'],'家居日用':['收纳整理','清洁用品','餐厨用品','雨具'],'家饰布艺':['窗帘','地毯','靠垫抱枕','装饰摆件'],'厨具':['锅具','刀具','餐具','烘焙用具']},
'家纺':{'床上用品':['床品套件/四件套','被子/被芯','枕头/枕芯','床垫/床褥','床单/床笠','被套','枕套','毛毯/盖毯','蚊帐','凉席'],'卫浴纺织':['毛巾','浴巾','浴袍','地垫'],'家居布艺':['窗帘','沙发垫','桌布','靠垫'],'儿童家纺':['婴童床品','儿童被','儿童枕'],'酒店家纺':['酒店布草','酒店毛巾','酒店被芯']},
'母婴玩具':{'婴儿用品':['奶瓶奶嘴','纸尿裤','洗护用品','喂养用品'],'童装童鞋':['婴儿服','儿童服装','童鞋','儿童配饰'],'孕产用品':['孕妇装','产后护理','孕妇营养'],'玩具':['益智玩具','积木','毛绒玩具','模型玩具'],'童车童床':['婴儿推车','安全座椅','婴儿床']},
'数码':{'手机通讯':['智能手机','功能手机','手机配件','对讲机'],'电脑办公':['笔记本电脑','台式机','显示器','电脑配件'],'摄影摄像':['数码相机','镜头','摄像机','摄影配件'],'智能设备':['智能手表','智能手环','VR设备','智能家居'],'影音娱乐':['耳机','音箱','播放器','游戏设备']},
'家用电器':{'大家电':['电视','冰箱','洗衣机','空调'],'厨房电器':['电饭煲','空气炸锅','咖啡机','料理机'],'生活电器':['吸尘器','净化器','风扇','取暖器'],'个护电器':['电吹风','剃须刀','美容仪','电动牙刷'],'商用电器':['商用厨房设备','商用制冷','商用清洁']},
'运动户外':{'运动服饰':['运动上衣','运动裤','运动内衣','运动鞋'],'健身训练':['健身器械','瑜伽用品','运动护具'],'户外装备':['帐篷','睡袋','户外桌椅','照明工具'],'骑行':['自行车','骑行装备','骑行服饰'],'球类':['篮球','足球','羽毛球','乒乓球'],'游泳':['泳衣','泳镜','游泳装备']},
'汽车摩托':{'汽车用品':['座垫脚垫','车载电器','清洁养护','安全应急'],'汽车配件':['发动机配件','底盘配件','车灯','轮胎轮毂'],'摩托车':['摩托整车','摩托配件','骑行装备'],'新能源汽车':['充电设备','新能源配件','车载用品']},
'宠物用品':{'猫用品':['猫粮','猫砂','猫玩具','猫窝'],'狗用品':['狗粮','狗零食','狗玩具','狗窝'],'水族':['鱼缸','水族器材','鱼粮'],'小宠':['鸟类用品','仓鼠用品','兔类用品'],'宠物健康':['驱虫用品','护理用品','营养保健']},
'图书文娱':{'图书':['文学小说','童书','教育考试','生活艺术'],'文具':['书写工具','本册纸品','学生文具','美术用品'],'乐器':['吉他','键盘乐器','打击乐器','乐器配件'],'收藏娱乐':['动漫周边','卡牌','收藏品','桌游']},
'办公工业':{'办公设备':['打印机','扫描仪','投影设备','会议设备'],'办公耗材':['打印耗材','纸品','装订用品'],'工业品':['机械设备','电工电气','仪器仪表','劳保用品'],'包装物流':['包装材料','物流设备','标签标识']},
'珠宝钟表':{'珠宝首饰':['黄金','钻石','翡翠玉石','珍珠'],'时尚饰品':['项链','耳饰','手链','戒指'],'钟表':['男表','女表','智能手表','钟表配件'],'眼镜':['太阳镜','光学眼镜','眼镜配件']},
'医药健康':{'医疗器械':['家庭检测','康复护理','医用防护'],'健康护理':['成人护理','按摩理疗','计生用品'],'传统滋补':['参茸','燕窝','药食同源'],'药品':['中西成药','皮肤用药','家庭常备']},
'本地生活与服务':{'餐饮服务':['餐饮套餐','饮品甜点','外卖服务'],'休闲娱乐':['电影演出','运动健身','亲子娱乐'],'丽人服务':['美容','美发','美甲'],'生活服务':['家政','维修','洗护'],'旅行':['酒店','景区门票','旅游线路']}
};
const categoryTreeEn={
'Apparel, Shoes & Bags':{'Women’s Clothing':['Dresses','Shirts','Knitwear','Coats','Pants','Skirts'],'Men’s Clothing':['T-Shirts','Shirts','Jackets','Suits','Pants'],'Underwear':['Bras','Underwear','Sleepwear','Socks'],'Shoes':['Women’s Shoes','Men’s Shoes','Kids’ Shoes','Athletic Shoes'],'Bags & Luggage':['Handbags','Men’s Bags','Luggage','Functional Bags']},
'Beauty & Personal Care':{'Skin Care':['Face Care','Masks','Sun Care','Eye Care'],'Makeup':['Face Makeup','Lip Makeup','Eye Makeup','Beauty Tools'],'Hair Care':['Shampoo','Conditioner','Styling'],'Body Care':['Bath','Body Lotion','Hand & Foot Care'],'Fragrance':['Women’s Fragrance','Men’s Fragrance','Home Fragrance']},
'Food & Grocery':{'Snacks':['Nuts','Candy & Chocolate','Cookies & Cakes','Meat Snacks'],'Pantry':['Rice & Grains','Cooking Oil','Condiments','Instant Food'],'Beverages':['Tea','Coffee','Milk Drinks','Water'],'Fresh Food':['Fruit','Vegetables','Meat & Eggs','Seafood'],'Alcohol':['Spirits','Wine','Beer']},
'Home & Furniture':{'Furniture':['Bedroom Furniture','Living Room Furniture','Dining Furniture','Office Furniture'],'Home Improvement':['Lighting','Kitchen & Bath Fixtures','Hardware Tools','Flooring'],'Household':['Storage','Cleaning Supplies','Kitchen & Dining','Rain Gear'],'Home Décor':['Curtains','Rugs','Cushions','Decorative Objects']},
'Home & Kitchen':{'Bedding':['Duvet Covers & Sets','Flat Bed Sheets','Fitted Bed Sheets','Pillowcases','Bed Pillows','Comforters & Sets','Mattress Pads & Toppers','Blankets & Throws'],'Bath Textiles':['Towels','Bath Towels','Bathrobes','Bath Mats'],'Home Fabrics':['Curtains','Sofa Covers','Table Linens','Cushion Covers'],'Kids’ Bedding':['Nursery Bedding','Kids’ Comforters','Kids’ Pillows'],'Hospitality Linens':['Hotel Bedding','Hotel Towels','Hotel Comforters']},
'Baby, Kids & Toys':{'Baby Care':['Bottles & Nipples','Diapers','Baby Skin Care','Feeding'],'Kids’ Apparel':['Baby Clothing','Kids’ Clothing','Kids’ Shoes','Accessories'],'Maternity':['Maternity Clothing','Postpartum Care','Maternity Nutrition'],'Toys':['Learning Toys','Building Blocks','Plush Toys','Models'],'Strollers & Nursery':['Strollers','Car Seats','Cribs']},
'Electronics':{'Phones':['Smartphones','Feature Phones','Phone Accessories','Two-Way Radios'],'Computers':['Laptops','Desktops','Monitors','Computer Components'],'Cameras':['Digital Cameras','Lenses','Camcorders','Camera Accessories'],'Smart Devices':['Smart Watches','Fitness Trackers','VR Devices','Smart Home'],'Audio & Gaming':['Headphones','Speakers','Media Players','Gaming']},
'Home Appliances':{'Major Appliances':['TVs','Refrigerators','Washers','Air Conditioners'],'Kitchen Appliances':['Rice Cookers','Air Fryers','Coffee Machines','Food Processors'],'Household Appliances':['Vacuums','Air Purifiers','Fans','Heaters'],'Personal Care Appliances':['Hair Dryers','Shavers','Beauty Devices','Electric Toothbrushes']},
'Sports & Outdoors':{'Sportswear':['Sports Tops','Sports Pants','Sports Bras','Athletic Shoes'],'Fitness':['Fitness Equipment','Yoga','Protective Gear'],'Outdoor Recreation':['Tents','Sleeping Bags','Camping Furniture','Outdoor Lighting'],'Cycling':['Bicycles','Cycling Gear','Cycling Apparel'],'Ball Sports':['Basketball','Soccer','Badminton','Table Tennis']},
'Automotive':{'Car Accessories':['Seat Covers & Mats','Car Electronics','Car Care','Emergency Supplies'],'Auto Parts':['Engine Parts','Chassis Parts','Lighting','Tires & Wheels'],'Motorcycle':['Motorcycles','Parts','Riding Gear'],'EV Accessories':['Charging Equipment','EV Parts','Vehicle Accessories']},
'Pet Supplies':{'Cats':['Cat Food','Cat Litter','Cat Toys','Cat Beds'],'Dogs':['Dog Food','Dog Treats','Dog Toys','Dog Beds'],'Aquatics':['Aquariums','Aquarium Equipment','Fish Food'],'Small Animals':['Bird Supplies','Hamster Supplies','Rabbit Supplies'],'Pet Health':['Flea & Tick','Grooming','Supplements']},
'Books & Entertainment':{'Books':['Literature','Children’s Books','Education','Arts & Lifestyle'],'Stationery':['Writing','Paper Products','School Supplies','Art Supplies'],'Musical Instruments':['Guitars','Keyboards','Percussion','Accessories'],'Collectibles & Games':['Anime','Trading Cards','Collectibles','Board Games']},
'Office & Industrial':{'Office Equipment':['Printers','Scanners','Projectors','Conference Equipment'],'Office Supplies':['Ink & Toner','Paper','Binding'],'Industrial':['Machinery','Electrical','Test Instruments','Safety'],'Packaging & Logistics':['Packaging','Material Handling','Labels']},
'Jewelry & Watches':{'Fine Jewelry':['Gold','Diamonds','Jade','Pearls'],'Fashion Jewelry':['Necklaces','Earrings','Bracelets','Rings'],'Watches':['Men’s Watches','Women’s Watches','Smart Watches','Accessories'],'Eyewear':['Sunglasses','Optical Eyewear','Accessories']},
'Health':{'Medical Devices':['Home Tests','Rehabilitation','Medical Protection'],'Personal Health':['Adult Care','Massage & Therapy','Family Planning'],'Supplements':['Vitamins','Nutrition','Herbal Supplements'],'OTC':['Pain Relief','Skin Care Medicine','First Aid']},
'Local Services':{'Food & Dining':['Restaurant Deals','Drinks & Desserts','Delivery'],'Entertainment':['Movies & Shows','Fitness','Family Activities'],'Beauty Services':['Beauty','Hair','Nails'],'Home Services':['Cleaning','Repair','Laundry'],'Travel':['Hotels','Attractions','Tours']}
};
const officialEnglishToChinese={};(()=>{const zhTop=Object.entries(categoryTreeZh),enTop=Object.entries(categoryTreeEn);enTop.forEach(([en1,enChildren],i)=>{const [zh1,zhChildren]=zhTop[i]||[en1,{}];officialEnglishToChinese[en1]=zh1;const zhSecond=Object.entries(zhChildren);Object.entries(enChildren).forEach(([en2,enLeaves],j)=>{const [zh2,zhLeaves]=zhSecond[j]||[en2,[]];officialEnglishToChinese[en2]=zh2;enLeaves.forEach((leaf,k)=>officialEnglishToChinese[leaf]=zhLeaves[k]||leaf)})})})();
Object.assign(officialEnglishToChinese,{'Home & Kitchen':'家居与厨房','Bedding':'床上用品','Duvet Covers & Sets':'被套及套装','Flat Bed Sheets':'床单','Fitted Bed Sheets':'床笠','Pillowcases':'枕套','Bed Pillows':'枕芯','Comforters & Sets':'被子及套装','Mattress Pads & Toppers':'床垫保护垫','Blankets & Throws':'毛毯和盖毯'});
const bilingualCategoryLabel=value=>officialEnglishToChinese[value]?`${officialEnglishToChinese[value]}（${value}）`:value;
const en={
'目标客群 / 价格':'Audience / Pricing',
'沉淀品牌、平台与商品运营规则，让每次内容生产保持一致。':'Define brands, platforms and operating rules for consistent content.','配置文案与图片任务使用的 AI 模型，检测后自动登记整组能力。':'Configure AI models for copy and image tasks.','把多个模型按业务用途组合成稳定、可复用的调用方案。':'Combine models into reusable execution plans.','为家纺运营岗位配置职责、方案和对应的业务范围。':'Configure roles, plans and business scope for each Agent.',
'业务配置':'Business Settings','模型管理':'Model Management','方案配置':'Plan Settings','Agent 管理':'Agent Management','业务':'Business','模型':'Model','方案':'Plan','Agent':'Agent','新增':'Add ','编辑':'Edit ','列表':' List','业务主体':'Business','平台与品类':'Platform / Category','目标客群 / 价格带':'Audience / Pricing','状态':'Status','服务商 / 标识':'Provider / ID','能力登记':'Capabilities','检测状态':'Health','类型':'Type','模型顺序':'Models','岗位 / 职责':'Role / Responsibilities','执行方案':'Plans','所属业务':'Business','显示名称':'Display Name','服务商':'Provider','主要能力':'Primary Capability','接口格式':'API Format','模型标识':'Model ID','接口地址':'Base URL','API Key（保存在本机，不回显）':'API Key (saved locally, never shown)','专用密钥变量（可选，以 ECOMMERCE2_MODEL_KEY_ 开头）':'API Key Environment Variable (optional)','图片生成 API 路径（如 /images/generations）':'Image Generation API Path','图片编辑 API 路径（如 /images/edits）':'Image Edit API Path','业务名称':'Business Name','品牌 / 店铺':'Brand / Store','运营平台':'Platform','平台主营类目（支持搜索选择）':'Platform Category (searchable)','目标客群':'Target Audience','成本价（元）':'Cost Price (CNY)','销售价（元）':'Sale Price (CNY)','视觉风格':'Visual Style','运营规则':'Operating Rules','方案名称':'Plan Name','方案类型':'Plan Type','使用模型（可多选）':'Models (multiple)','方案说明':'Description','Agent 名称':'Agent Name','岗位角色':'Role','职责与工作要求':'Responsibilities','执行方案（可多选）':'Plans (multiple)','启用此业务':'Enable Business','启用此模型':'Enable Model','启用此方案':'Enable Plan','启用此 Agent':'Enable Agent','文本':'Text','图片':'Image','商品文案':'Product Copy','图片生成':'Image Generation','图片精修':'Image Retouching','竞品分析':'Competitor Analysis','客服回复':'Customer Service','通用':'General','已启用':'Enabled','未启用':'Disabled','检测正常':'Healthy','检测异常':'Error','待检测':'Untested','取消':'Cancel','保存配置':'Save','搜索名称或配置内容':'Search configurations','配置完成后即可用于后续产品运营流程':'Configurations will power subsequent product operations','抖音':'Douyin','淘宝':'Taobao','天猫':'Tmall','京东':'JD.com','拼多多':'Pinduoduo','小红书':'RED','视频号':'WeChat Channels','快手':'Kuaishou','亚马逊':'Amazon','独立站':'Independent Store','其他':'Other'};
let language='zh';
const tx=s=>language==='en'?(en[s]||s):s;
let page=document.body?.dataset?.initialPage||'businesses',items={},editing=null,editorSession=0,configurationSavePending=false;
window.workspaceRole=document.body?.dataset?.workspaceRole||null;
const $=s=>document.querySelector(s); const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const pendingApiWrites=new Map();
const transientHttp=new Set([502,503,504,520,521,522,523,524,530]);
const retryableWorkspaceReads=new Set(['/api/auth/session','/api/commerce/overview?summary=1','/api/commerce/overview/revision',
  ...Object.keys(pages).map(key=>'/api/'+key+(key==='design_projects'?'?summary=1':''))]);
function api(url,options={}){
  const method=(options.method||'GET').toUpperCase();
  const key=!['GET','HEAD'].includes(method)&&typeof options.body==='string'?method+' '+url+' '+options.body:null;
  if(key&&pendingApiWrites.has(key))return pendingApiWrites.get(key);
  const request=(async()=>{
    const headers={...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...options.headers};
    const safe=method==='GET'&&retryableWorkspaceReads.has(url)&&!options.signal;
    const reading=method==='GET'||method==='HEAD';
    for(let attempt=0;;attempt++){
      const controller=safe&&typeof AbortController!=='undefined'?new AbortController():null;
      const timeout=controller?setTimeout(()=>controller.abort(),12000):null;
      let response;
      try{
        response=await fetch(url,{credentials:'same-origin',cache:'no-store',...options,headers,...(controller?{signal:controller.signal}:{})});
        if(response.ok)return response.status===204?null:await response.json();
        if(response.status===401){
          workspaceAccessExpired=true;
          for(const node of document.querySelectorAll('#content,#stats,dialog'))node.replaceChildren();
          location.replace('/commerce-login');
        }
        let d={};try{d=await response.json()}catch{}
        const detail=typeof d?.detail==='string'?d.detail:Array.isArray(d?.detail)?d.detail.map(x=>[Array.isArray(x.loc)?x.loc.filter(v=>!['body','query','path'].includes(v)).join(' · '):'',x.msg].filter(Boolean).join('：')).filter(Boolean).join('；'):'';
        const ray=response.headers?.get?.('cf-ray');
        const trace=ray&&/^[\w-]{1,64}$/.test(ray)?' · '+ray:'';
        const message=reading?'读取失败':'操作未完成';
        const fallback=!reading&&transientHttp.has(response.status)?'操作结果待核实，请先查看当前记录':message;
        throw Error(detail||`${fallback}（${response.status}${trace}）`);
      }catch(error){
        if(options.signal?.aborted||workspaceAccessExpired)throw error;
        const transport=!response||response.ok;
        if(!safe||attempt>0||(!transport&&!transientHttp.has(response.status))){
          if(transport)throw Error(reading?'未能读到完整数据，请重新读取。':'未收到完整的操作回执，请先查看当前记录再决定是否重试。');
          throw error;
        }
      }finally{if(timeout!==null)clearTimeout(timeout);}
      await new Promise(resolve=>setTimeout(resolve,650));
    }
  })();
  if(key){pendingApiWrites.set(key,request);request.then(()=>pendingApiWrites.delete(key),()=>pendingApiWrites.delete(key));}
  return request;
}
function ensureUIActionDialog(){
  if(document.querySelector('#uiActionDialog'))return;
  document.body.insertAdjacentHTML('beforeend',`<dialog id="uiActionDialog" class="classification-dialog ui-action-dialog"><form id="uiActionForm"><div class="dialog-head"><div><p class="eyebrow">ACTION CONFIRMATION</p><h2 id="uiActionTitle">确认操作</h2></div><button type="button" class="icon-btn" id="uiActionClose">×</button></div><div class="classification-content"><p id="uiActionMessage" class="ui-action-message"></p><label id="uiActionInputWrap" class="ai-design-field" hidden><span id="uiActionInputLabel">补充说明</span><textarea id="uiActionInput" rows="5"></textarea></label><div id="uiActionError" class="ui-action-error" hidden></div></div><div class="dialog-foot"><button type="button" class="secondary" id="uiActionCancel">取消</button><button type="submit" class="primary" id="uiActionConfirm">确认</button></div></form></dialog>`);
}
function openUIActionDialog(options={}){
  ensureUIActionDialog();const dialog=$('#uiActionDialog'),form=$('#uiActionForm'),input=$('#uiActionInput'),inputWrap=$('#uiActionInputWrap'),error=$('#uiActionError'),wantsInput=Boolean(options.input);
  if(dialog.open&&dialog._finish)dialog._finish(wantsInput?null:false);
  $('#uiActionTitle').textContent=options.title||'确认操作';$('#uiActionMessage').textContent=options.message||'';$('#uiActionConfirm').textContent=options.confirmLabel||'确认';$('#uiActionConfirm').className=options.danger?'delete':'primary';$('#uiActionCancel').textContent=options.cancelLabel||'取消';inputWrap.hidden=!wantsInput;$('#uiActionInputLabel').textContent=options.inputLabel||'补充说明';input.placeholder=options.placeholder||'';input.value=options.initialValue||'';error.hidden=true;error.textContent='';
  return new Promise(resolve=>{let settled=false;const finish=value=>{if(settled)return;settled=true;dialog._finish=null;if(dialog.open)dialog.close();resolve(value)};dialog._finish=finish;$('#uiActionClose').onclick=()=>finish(wantsInput?null:false);$('#uiActionCancel').onclick=()=>finish(wantsInput?null:false);dialog.oncancel=event=>{event.preventDefault();finish(wantsInput?null:false)};form.onsubmit=event=>{event.preventDefault();if(wantsInput){const value=input.value.trim(),minLength=Number(options.minLength||0);if(value.length<minLength){error.textContent=options.validationMessage||`请至少填写 ${minLength} 个字。`;error.hidden=false;input.focus();return}finish(value)}else finish(true)};dialog.showModal();queueMicrotask(()=>wantsInput?input.focus():$('#uiActionConfirm').focus())});
}
function uiConfirm(message,options={}){return openUIActionDialog({...options,message,input:false})}
function uiPrompt(message,options={}){return openUIActionDialog({...options,message,input:true})}
window.uiConfirm=uiConfirm;window.uiPrompt=uiPrompt;
let workspaceAccessExpired=false,workspaceSessionError=null,workspaceLoadVersion=0;
const workspaceReadErrors=new Map();
const workspacePending=new Set();
const workspaceDependencies={
  businesses:['platform_accounts'],plans:['models'],agents:['plans','businesses'],
  commerce_home:['design_projects'],commerce_products:['businesses'],commerce_listings:['platform_accounts'],commerce_support:['plans']
};
function pageCollections(){
  return [...(Object.hasOwn(pages,page)?[page]:[]),...(workspaceDependencies[page]||[])];
}
function renderWorkspaceReadState(){
  if(workspaceAccessExpired)return true;
  const unloaded=pageCollections().filter(key=>!Object.hasOwn(items,key)&&!workspacePending.has(key)&&!workspaceReadErrors.has(key));
  if(window.workspaceDataLoaded&&unloaded.length){
    for(const key of unloaded)workspacePending.add(key);
    readCollections(unloaded,workspaceLoadVersion).then(()=>render());
  }
  const dependencies=pageCollections(),waiting=dependencies.some(key=>workspacePending.has(key)&&!Object.hasOwn(items,key));
  const affected=dependencies.filter(key=>workspaceReadErrors.has(key));
  if(!workspaceSessionError&&!affected.length&&!waiting)return false;
  const message=workspaceSessionError||affected.map(key=>pages[key].title+' · '+workspaceReadErrors.get(key)).join(' / ');
  $('#stats').replaceChildren();$('#add').hidden=true;
  const mapAll=document.querySelector('[data-business-map="all"]');if(mapAll)mapAll.hidden=true;
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  $('#listTitle').textContent=workspaceSessionError?'工作台':(pages[page]?.title||'工作台资料');
  if(waiting&&!workspaceSessionError){$('#content').innerHTML='<div class="empty" role="status">正在读取本页资料…</div>';return true;}
  $('#panelHint').textContent='连接恢复后可以在这里重新读取';
  $('#content').innerHTML='<div class="empty" role="status"><p>'+esc(message)+'</p><button type="button" class="secondary" data-retry-workspace>重新读取</button></div>';
  $('#content [data-retry-workspace]').onclick=event=>{
    event.currentTarget.disabled=true;
    if(workspaceSessionError){load();return;}
    readCollections(affected,workspaceLoadVersion).then(()=>render());
  };
  return true;
}
async function readCollections(keys,version,progressive=false){
  await Promise.all(keys.map(async key=>{
    try{
      const result=await api('/api/'+key+(key==='design_projects'?'?summary=1':''));
      if(!Array.isArray(result))throw Error('读取的数据格式异常，请重新读取。');
      if(version===workspaceLoadVersion&&!workspaceAccessExpired){items[key]=result;workspaceReadErrors.delete(key);}
    }catch(error){if(version===workspaceLoadVersion)workspaceReadErrors.set(key,error.message);}
    finally{
      if(version===workspaceLoadVersion){
        workspacePending.delete(key);
        if(progressive&&pageCollections().includes(key)&&!workspaceAccessExpired)render();
      }
    }
  }));
}
async function load(){
  const version=++workspaceLoadVersion;
  try{
  const session=await api('/api/auth/session');
  if(version!==workspaceLoadVersion||workspaceAccessExpired)return;
  workspaceSessionError=null;
  window.workspaceRole=session.authenticated?session.role:'admin';
  document.body.dataset.workspaceRole=window.workspaceRole;
  const operator=window.workspaceRole==='operator';
  // Operators still need business, account and plan choices for commerce forms.
  // Model configuration is administrator-only and must not fail the whole load.
  const required=pageCollections();
  const keys=Object.keys(pages).filter(k=>(!operator||k!=='models')&&(k!=='design_projects'||required.includes(k)));
  if(operator)items.models=[];
  // Page renderers are loaded by later scripts in index.html.
  if(document.readyState==='loading')await new Promise(resolve=>document.addEventListener('DOMContentLoaded',resolve,{once:true}));
  if(version!==workspaceLoadVersion||workspaceAccessExpired)return;
  if(operator&&!page.startsWith('commerce_'))page='commerce_home';
  const first=!window.workspaceDataLoaded;
  window.workspaceDataLoaded=true;
  for(const key of keys)workspacePending.add(key);
  if(first)render();
  await readCollections(keys,version,first);
  if(!first&&version===workspaceLoadVersion&&!workspaceAccessExpired)render();
  }catch(e){
    if(version!==workspaceLoadVersion||workspaceAccessExpired)return;
    workspaceSessionError=e.message;
    renderWorkspaceReadState();
  }
}
function applyLanguage(){const nav={businesses:'业务配置',models:'模型管理',plans:'方案配置',agents:'Agent 管理'};document.querySelectorAll('.nav-item').forEach(b=>{const label=b.querySelector('.nav-label');if(label&&nav[b.dataset.page])label.textContent=nav[b.dataset.page]});const hint=$('#panelHint'),search=$('#search'),cancel=$('#cancelButton'),saveButton=$('#saveButton');if(hint)hint.textContent='配置完成后即可用于后续产品运营流程';if(search)search.placeholder='搜索名称或配置内容';if(cancel)cancel.textContent='取消';if(saveButton)saveButton.textContent='保存配置';document.documentElement.lang='zh-CN'}
function render(){
  if(!window.workspaceDataLoaded||!Object.hasOwn(items,page))return;
  $('#add').hidden=false;applyLanguage();
  const cfg=pages[page],q=$('#search').value.trim().toLowerCase();
  $('#title').textContent=tx(cfg.title);$('#subtitle').textContent=cfg.subtitle;
  $('#addLabel').textContent=language==='en'?tx('新增')+tx(cfg.singular):'新增'+cfg.singular;
  $('#listTitle').textContent=language==='en'?tx(cfg.title)+tx('列表'):cfg.title+'列表';
  document.querySelectorAll('.nav-item').forEach(x=>x.classList.toggle('active',x.dataset.page===page));
  const list=(items[page]||[]).filter(x=>JSON.stringify(x).toLowerCase().includes(q));
  if(page==='supplier_imports'&&window.renderSupplierImportCards){
    $('#addLabel').textContent='上传供应商货盘';$('#listTitle').textContent='货盘导入记录';$('#search').placeholder='搜索供应商、文件或导入状态';
    const counts=[['导入批次',list.length],['已确认入库',list.filter(x=>x.status==='映射已确认').length],['处理中',list.filter(x=>['AI排队中','AI解析中','等待重试'].includes(x.status)).length],['已归档批次',list.filter(x=>x.status==='已归档').length]];
    $('#stats').innerHTML=counts.map(([title,n])=>`<div class="stat"><small>${title}</small><b>${n}</b></div>`).join('');
    $('#content').innerHTML=window.renderSupplierImportCards(list);return;
  }
  const enabled=list.filter(x=>x.enabled).length;
  $('#stats').innerHTML=`<div class="stat"><small>${language==='en'?'Total':'全部配置'}</small><b>${list.length}</b></div><div class="stat"><small>${language==='en'?'Enabled':'当前启用'}</small><b>${enabled}</b></div><div class="stat"><small>${language==='en'?'Disabled':'暂未启用'}</small><b>${list.length-enabled}</b></div>`;
  if(!list.length){$('#content').innerHTML=`<div class="empty"><b>${language==='en'?'No matching configurations':'还没有匹配的配置'}</b>${language==='en'?'Use the button above to create one':'点击右上角按钮创建第一条记录'}</div>`;return;}
  $('#content').innerHTML=`<table><thead><tr>${cfg.columns.map(x=>`<th>${tx(x)}</th>`).join('')}<th>${language==='en'?'Actions':'操作'}</th></tr></thead><tbody>${list.map(row).join('')}</tbody></table>`;
}
function nameOf(kind,id){return (items[kind]||[]).find(x=>x.id===id)?.name||'—'}
function status(x){return `<span class="badge ${x.enabled?'':'off'}">${x.enabled?'已启用':'未启用'}</span>`}
function row(x){let cells=[];if(page==='businesses'){const platforms=(x.platforms||[]).join('、')||'未设置平台',paths=(x.category_paths||[]).join('；')||'未设置类目',combo=x.set_type||'单件',components=(x.components||[]).join('＋')||x.name,cells=[`<div class="item-name">${esc(x.name)}</div><div class="sub">${esc(combo)} · ${x.piece_count||1} 件</div>`,`<div>${esc(platforms)}</div><div class="sub">${esc(paths)}</div>`,`<div>成本 ¥${x.cost_price??'—'} · 销售 ¥${x.sale_price??'—'}</div><div class="sub">${esc(components)}</div>`,status(x)];return `<tr>${cells.map(c=>`<td>${c}</td>`).join('')}<td><div class="actions"><button onclick="openEditor(${x.id})">编辑</button><button class="delete" onclick="removeItem(${x.id})">删除</button></div></td></tr>`}if(page==='models'){const health={healthy:'检测正常',error:'检测异常',untested:'待检测'}[x.health_status]||'待检测';cells=[`<div class="item-name">${esc(x.name)}</div><div class="sub">${esc(x.base_url||'未填写接口')}</div>`,`${esc(x.provider)}<div class="sub">${esc(x.model_identifier)}</div>`,`<span class="badge">${esc(x.capability)}</span><div class="sub">${esc((x.detected_capabilities||[]).join('、'))}</div>`,`<span class="badge ${x.health_status==='error'?'off':''}">${health}</span><div class="sub">${esc(x.health_message||'尚未进行实际调用')}</div>`]}if(page==='plans')cells=[`<div class="item-name">${esc(x.name)}</div><div class="sub">${esc(x.description||'暂无说明')}</div>`,`<span class="badge">${esc(x.plan_type)}</span>`,(x.model_ids||[]).map(id=>esc(nameOf('models',id))).join(' → ')||'—',status(x)];if(page==='agents')cells=[`<div class="item-name">${esc(x.name)}</div>`,`${esc(x.role||'未设置岗位')}<div class="sub">${esc(x.responsibilities||'暂无职责说明')}</div>`,(x.plan_ids||[]).map(id=>esc(nameOf('plans',id))).join('、')||'—',esc(nameOf('businesses',x.business_id)),status(x)];return `<tr>${cells.map(c=>`<td>${c}</td>`).join('')}<td><div class="actions">${page==='models'?`<button onclick="quickTest(${x.id})">检测</button>`:''}<button onclick="openEditor(${x.id})">编辑</button><button class="delete" onclick="removeItem(${x.id})">删除</button></div></td></tr>`}
function fieldHtml(f,item){const [key,label,type,extra]=f,v=item?.[key];if(type==='checkbox')return `<label class="field full check" data-field="${key}"><input name="${key}" type="checkbox" ${v!==false?'checked':''}> ${label}</label>`;let control='';if(type==='textarea')control=`<textarea name="${key}" placeholder="请输入${label}">${esc(v)}</textarea>`;else if(type==='select')control=`<select name="${key}">${extra.map(o=>`<option ${v===o?'selected':''}>${o}</option>`).join('')}</select>`;else if(type==='selectValue')control=`<select name="${key}">${extra.map(o=>`<option value="${o[0]}" ${v===o[0]?'selected':''}>${o[1]}</option>`).join('')}</select>`;else if(type==='multiselect')control=`<div class="model-check-list" data-model-choices data-selected="${esc(JSON.stringify(v||[]))}"></div>`;else if(type==='planmulti')control=`<div class="model-check-list">${(items.plans||[]).filter(p=>p.enabled).map(p=>`<label class="model-check"><input type="checkbox" name="${key}" value="${p.id}" ${(v||[]).includes(p.id)?'checked':''}><span>${esc(p.name)}</span><small>${esc(p.plan_type)}</small></label>`).join('')||'<div class="model-check-empty">暂无可用方案</div>'}</div>`;else if(type==='plan'||type==='business'){const kind=type==='plan'?'plans':'businesses';control=`<select name="${key}"><option value="">暂不指定</option>${(items[kind]||[]).map(o=>`<option value="${o.id}" ${v===o.id?'selected':''}>${esc(o.name)}</option>`).join('')}</select>`}else{const placeholder=type==='password'&&item?.has_credential?'已保存，留空保持不变':`请输入${label}`;control=`<input name="${key}" type="${type}" value="${type==='password'?'':esc(v)}" ${extra===true?'required':''} placeholder="${placeholder}">`}return `<div class="field ${['textarea','multiselect','planmulti'].includes(type)?'full':''}" data-field="${key}"><label>${label}${extra===true?' *':''}</label>${control}</div>`}
function openEditor(id=null){editorSession++;editing=id;const item=id?(items[page]||[]).find(x=>x.id===id):null;$('#dialogTitle').textContent=(id?'编辑':'新增')+pages[page].singular;$('#fields').innerHTML=pages[page].fields.map(f=>fieldHtml(f,item)).join('')+(page==='models'?`<div id="officialAccountBox" class="field full probe-box official-account" hidden><label>OpenAI 官方账号授权</label><div id="officialAccountStatus" class="account-status loading">正在读取账号状态…</div><div class="probe-actions"><button id="connectOpenAI" class="primary" type="button" onclick="startOpenAIAccountLogin()">连接 ChatGPT 账号</button><button class="secondary" type="button" onclick="loadOpenAIAccount(true)">刷新状态</button><button id="disconnectOpenAI" class="secondary danger" type="button" onclick="disconnectOpenAIAccount()" hidden>退出账号</button></div><small>使用 Codex 官方托管 OAuth；应用不会读取或保存账号 Token。Pro 套餐用量与 API Key 计费相互独立。</small></div><div class="field full probe-box"><label>模型检索与连接检测</label><div class="probe-actions"><button id="discoverModelsButton" class="secondary" type="button" onclick="discoverModels()">获取可用模型</button><button class="secondary" type="button" onclick="testConnection()">测试连接（实际调用）</button><select id="discovered" hidden></select></div><small id="probeHelp">文本会发送极短消息；图片 API 会真实生成测试图，可能产生少量费用。</small><div id="probeResult" class="probe-result" hidden></div></div>`:'');if(page==='platform_accounts')$('#fields').insertAdjacentHTML('beforeend',`<div class="field full"><p>账号保存后，在平台同步中点击“授权配置”填写 Client ID、Client Secret、Refresh Token，并配置地区。</p>${id?`<button type="button" onclick="document.querySelector('#editor').close();commerceAuthorization(${id})">打开授权配置（不保存本次编辑）</button>`:''}</div>`);if(page==='models'){const form=$('#form');form.elements.provider.addEventListener('change',()=>syncOfficialAccountFields(true));form.elements.capability.addEventListener('change',()=>syncModelFields(true));form.elements.model_identifier.addEventListener('change',()=>applyModelKind(form.elements.model_identifier.value));syncOfficialAccountFields(false)}if(page==='plans'){const type=$('#form').elements.plan_type;document.querySelector('[data-field="plan_type"]').insertAdjacentHTML('afterend','<div id="planTypeHelp" class="field full plan-type-help"></div>');type.addEventListener('change',()=>{renderPlanModelChoices([]);updatePlanTypeHelp()});renderPlanModelChoices(item?.model_ids||[]);updatePlanTypeHelp()}$('#editor').showModal()}
function renderPlanModelChoices(initial=null){const box=document.querySelector('[data-model-choices]');if(!box)return;const selected=initial??[...box.querySelectorAll('input:checked')].map(x=>Number(x.value)),planType=$('#form').elements.plan_type.value,wantsImage=/图片/.test(planType),available=(items.models||[]).filter(m=>m.enabled&&m.capability===(wantsImage?'图片':'文本')),byId=new Map(available.map(m=>[m.id,m])),ordered=[...selected.map(id=>byId.get(id)).filter(Boolean),...available.filter(m=>!selected.includes(m.id))];box.innerHTML=ordered.length?`<div class="model-order-help">拖动左侧手柄或使用箭头调整顺序；已勾选模型从上到下保存为优先顺序。</div>`+ordered.map(m=>`<div class="model-check model-order-item" draggable="true" data-model-id="${m.id}"><span class="model-drag" title="拖动排序">⋮⋮</span><label class="model-choice-main"><input type="checkbox" name="model_ids" value="${m.id}" ${selected.includes(m.id)?'checked':''}><span>${esc(m.name)}</span></label><small data-model-rank>${esc(m.capability)}</small><div class="model-order-actions"><button type="button" title="上移" onclick="movePlanModel(${m.id},-1)">↑</button><button type="button" title="下移" onclick="movePlanModel(${m.id},1)">↓</button></div></div>`).join(''):`<div class="model-check-empty">暂无可用的${wantsImage?'图片':'文本'}模型，请先到模型管理中配置</div>`;initPlanModelOrdering()}
function refreshPlanModelRanks(){const box=document.querySelector('[data-model-choices]');if(!box)return;let rank=0;box.querySelectorAll('.model-order-item').forEach(row=>{const checked=row.querySelector('input').checked,label=row.querySelector('[data-model-rank]');row.classList.toggle('selected',checked);label.textContent=checked?`优先级 ${++rank}`:'未启用'})}
function movePlanModel(id,direction){const row=document.querySelector(`.model-order-item[data-model-id="${id}"]`);if(!row)return;const target=direction<0?row.previousElementSibling:row.nextElementSibling;if(!target||!target.classList.contains('model-order-item'))return;if(direction<0)row.parentElement.insertBefore(row,target);else row.parentElement.insertBefore(target,row);refreshPlanModelRanks()}
function initPlanModelOrdering(){const box=document.querySelector('[data-model-choices]');if(!box)return;let dragged=null;box.querySelectorAll('.model-order-item').forEach(row=>{row.addEventListener('dragstart',e=>{dragged=row;row.classList.add('dragging');e.dataTransfer.effectAllowed='move'});row.addEventListener('dragend',()=>{row.classList.remove('dragging');dragged=null;refreshPlanModelRanks()});row.addEventListener('dragover',e=>{e.preventDefault();if(!dragged||dragged===row)return;const rect=row.getBoundingClientRect(),after=e.clientY>rect.top+rect.height/2;box.insertBefore(dragged,after?row.nextSibling:row)});row.querySelector('input').addEventListener('change',refreshPlanModelRanks)});refreshPlanModelRanks()}
function updatePlanTypeHelp(){const box=$('#planTypeHelp'),type=$('#form')?.elements?.plan_type?.value;if(!box)return;const help={图片生成:['图片生成','创建新的完整画面','可根据商品原图或文字要求重做构图、背景、场景和视角；适合白底主图、场景图、不同角度图。'],图片精修:['图片精修','在现有图片上修得更好','尽量保持商品形态、颜色和纹理，只调整光影、背景、瑕疵、清晰度或局部区域；适合已有成片优化。']}[type];box.hidden=!help;if(help)box.innerHTML=`<b>${help[0]}</b><span>${help[1]}</span><small>${help[2]}</small>`}
function flattenCategoryTree(tree){const paths=[];for(const [level1,children] of Object.entries(tree))for(const [level2,leaves] of Object.entries(children))for(const level3 of leaves)paths.push(`${level1} > ${level2} > ${level3}`);return paths}
function prepareBusinessForm(){const form=$('#form'),platformInput=form.elements.platforms,categoryInput=form.elements.category_paths,defaultLanguage=form.elements.content_language;if(!platformInput||!categoryInput||platformInput.dataset.ready)return;platformInput.dataset.ready='1';platformInput.hidden=true;platformInput.removeAttribute('name');categoryInput.hidden=true;categoryInput.removeAttribute('name');const current=editing?(items.businesses||[]).find(x=>x.id===editing):null;let selectedPlatforms=[...(current?.platforms||[])],selectedCategories=[...(current?.category_paths||[])];const standard=['抖音','淘宝','天猫','京东','拼多多','小红书','视频号','1688','快手','亚马逊','Shopee','Lazada','Temu','独立站'];const platformBox=document.createElement('div');platformBox.className='business-manager';platformInput.after(platformBox);const categoryBox=document.createElement('div');categoryBox.className='business-manager category-manager';categoryInput.after(categoryBox);const renderPlatforms=()=>{const all=[...new Set([...standard,...selectedPlatforms])];platformBox.innerHTML=`<div class="platform-grid">${all.map(p=>`<label class="model-check"><input type="checkbox" value="${esc(p)}" ${selectedPlatforms.includes(p)?'checked':''}><span>${esc(p)}</span></label>`).join('')}</div><div class="manager-add"><input data-custom-platform placeholder="添加自定义平台"><button type="button" class="secondary" data-add-platform>添加平台</button></div>`;platformBox.querySelectorAll('input[type=checkbox]').forEach(box=>box.onchange=()=>{selectedPlatforms=box.checked?[...new Set([...selectedPlatforms,box.value])]:selectedPlatforms.filter(x=>x!==box.value);renderCategories()});platformBox.querySelector('[data-add-platform]').onclick=()=>{const input=platformBox.querySelector('[data-custom-platform]'),value=input.value.trim();if(value){selectedPlatforms=[...new Set([...selectedPlatforms,value])];renderPlatforms();renderCategories()}}};const renderCategories=()=>{const platformOptions=selectedPlatforms.length?selectedPlatforms:['未指定平台'];categoryBox.innerHTML=`<div class="category-tools"><select data-category-platform>${platformOptions.map(p=>`<option>${esc(p)}</option>`).join('')}</select><select data-category-language><option value="zh">中文</option><option value="en">English</option></select></div><div class="category-levels"><select data-category-level="1"></select><select data-category-level="2"></select><select data-category-level="3"></select></div><div class="manager-add"><input data-category-search list="allCategoryPaths" placeholder="搜索全部类目"><datalist id="allCategoryPaths"></datalist><button type="button" class="primary" data-add-category>添加类目</button></div><div class="selected-category-list">${selectedCategories.map((p,i)=>`<div class="selected-category"><span>${esc(p)}</span><button type="button" data-remove-category="${i}">×</button><input type="hidden" name="category_paths" value="${esc(p)}"></div>`).join('')||'<div class="model-check-empty">尚未添加类目</div>'}</div>${selectedPlatforms.map(p=>`<input type="hidden" name="platforms" value="${esc(p)}">`).join('')}`;const target=categoryBox.querySelector('[data-category-platform]'),lang=categoryBox.querySelector('[data-category-language]'),l1=categoryBox.querySelector('[data-category-level="1"]'),l2=categoryBox.querySelector('[data-category-level="2"]'),l3=categoryBox.querySelector('[data-category-level="3"]'),search=categoryBox.querySelector('[data-category-search]'),list=categoryBox.querySelector('datalist'),tree=()=>lang.value==='en'?categoryTreeEn:categoryTreeZh,option=(v,label=v)=>`<option value="${esc(v)}">${esc(label)}</option>`;lang.value=englishDefaultPlatforms.has(target.value)?'en':defaultLanguage.value;const fill3=()=>{const leaves=tree()[l1.value]?.[l2.value]||[];l3.innerHTML=option('','三级类目')+leaves.map(x=>option(x)).join('')};const fill2=()=>{const children=tree()[l1.value]||{};l2.innerHTML=option('','二级类目')+Object.keys(children).map(x=>option(x)).join('');fill3()};const rebuild=()=>{l1.innerHTML=option('','一级类目')+Object.keys(tree()).map(x=>option(x)).join('');fill2();list.innerHTML=flattenCategoryTree(tree()).map(x=>option(x)).join('');search.value=''};l1.onchange=fill2;l2.onchange=fill3;lang.onchange=rebuild;target.onchange=()=>{lang.value=englishDefaultPlatforms.has(target.value)?'en':defaultLanguage.value;rebuild()};categoryBox.querySelector('[data-add-category]').onclick=()=>{const path=search.value||[l1.value,l2.value,l3.value].filter(Boolean).join(' > ');if(!path||!target.value)return;selectedCategories=[...new Set([...selectedCategories,`${target.value}｜${path}`])];renderCategories()};categoryBox.querySelectorAll('[data-remove-category]').forEach(b=>b.onclick=()=>{selectedCategories.splice(Number(b.dataset.removeCategory),1);renderCategories()});rebuild()};renderPlatforms();renderCategories()}
function translateOpenForm(){if(language!=='en')return;document.querySelectorAll('#fields .field:not(.check)>label').forEach(label=>{const raw=label.textContent.replace(/\s*\*$/,'');label.textContent=tx(raw)+(label.textContent.includes('*')?' *':'')});document.querySelectorAll('#fields select:not(#discovered) option').forEach(option=>{const original=option.value;option.setAttribute('value',original);option.textContent=tx(option.textContent)});document.querySelectorAll('#fields .check').forEach(label=>{const node=[...label.childNodes].find(n=>n.nodeType===3&&n.textContent.trim());if(node)node.textContent=' '+tx(node.textContent.trim())});const title=$('#dialogTitle');if(title.textContent.startsWith('新增'))title.textContent='Add '+tx(title.textContent.slice(2));else if(title.textContent.startsWith('编辑'))title.textContent='Edit '+tx(title.textContent.slice(2))}
const configurableLanguageBusinessForm=prepareBusinessForm;prepareBusinessForm=function(){configurableLanguageBusinessForm();const form=$('#form'),hiddenLanguage=form.elements.content_language,manager=document.querySelector('.category-manager');if(!hiddenLanguage||!manager)return;const activeTarget=()=>manager.querySelector('[data-category-platform]'),activeLanguage=()=>manager.querySelector('[data-category-language]');const decorate=()=>{const target=activeTarget();if(!target||!englishDefaultPlatforms.has(target.value))return;manager.querySelectorAll('[data-category-level] option').forEach(option=>{if(!option.value||option.dataset.bilingual)return;const official=option.value;option.setAttribute('value',official);option.textContent=bilingualCategoryLabel(official);option.dataset.bilingual='1'});manager.querySelectorAll('datalist option').forEach(option=>{const official=option.value;if(!official||option.dataset.bilingual)return;option.setAttribute('value',official);option.setAttribute('label',official.split(' > ').map(x=>officialEnglishToChinese[x]||x).join(' > '));option.dataset.bilingual='1'})};const applyOfficialLanguage=()=>{const target=activeTarget(),languageSelect=activeLanguage();if(!target||!languageSelect)return;const english=englishDefaultPlatforms.has(target.value);hiddenLanguage.value=english?'en':'zh';languageSelect.value=english?'en':'zh';languageSelect.dispatchEvent(new Event('change'));let rule=manager.querySelector('[data-official-language]');if(!rule){rule=document.createElement('div');rule.dataset.officialLanguage='1';rule.className='category-rule';languageSelect.after(rule)}rule.textContent=english?'分类参考：中文与英文 · 上架分类使用本地平台映射':'分类参考：中文 · 上架分类使用本地平台映射';languageSelect.hidden=true;queueMicrotask(decorate)};new MutationObserver(()=>queueMicrotask(decorate)).observe(manager,{childList:true,subtree:true});manager.addEventListener('change',event=>{if(event.target.matches('[data-category-platform]'))applyOfficialLanguage()});applyOfficialLanguage()}
new MutationObserver(()=>queueMicrotask(translateOpenForm)).observe($('#fields'),{childList:true});
function toggleLanguage(){language=language==='zh'?'en':'zh';localStorage.setItem('shopLanguage',language);if($('#editor').open)$('#editor').close();render()}
window.toggleLanguage=toggleLanguage;
function looksLikeImageModel(id=''){return /(^|[-_/])(gpt-image|dall-e|imagen|flux|sdxl|stable-diffusion|image-gen)/i.test(id)}
const OFFICIAL_OPENAI_PROVIDER='OpenAI 官方账号（Pro）';
function isOfficialOpenAI(form=$('#form')){return form?.elements?.provider?.value===OFFICIAL_OPENAI_PROVIDER||form?.elements?.protocol?.value==='codex'}
function applyModelKind(id){const form=$('#form'),isImage=looksLikeImageModel(id);if(isImage)form.elements.capability.value='图片';if(!isOfficialOpenAI(form))form.elements.protocol.value=isImage?'images':'openai';syncModelFields(false)}
function syncOfficialAccountFields(changed=false){const form=$('#form'),official=form.elements.provider.value===OFFICIAL_OPENAI_PROVIDER;if(official){form.elements.protocol.value='codex';form.elements.base_url.value='codex://chatgpt';form.elements.api_key.value='';form.elements.api_key_env.value='';['base_url','api_key','api_key_env','image_generation_endpoint','image_edit_endpoint'].forEach(k=>{const box=document.querySelector(`[data-field="${k}"]`);if(box)box.hidden=true});$('#officialAccountBox').hidden=false;$('#discoverModelsButton').textContent='从官方账号获取模型';$('#probeHelp').textContent='连接检测使用当前 ChatGPT/Codex 套餐；IMAGE2.5 可选择并试用，图片生成成功后会注明实际型号是否确认。';loadOpenAIAccount()}else{if(changed&&form.elements.protocol.value==='codex')form.elements.protocol.value=form.elements.capability.value==='图片'?'images':'openai';['base_url','api_key','api_key_env'].forEach(k=>{const box=document.querySelector(`[data-field="${k}"]`);if(box)box.hidden=false});$('#officialAccountBox').hidden=true;$('#discoverModelsButton').textContent='根据 URL 和 Key 获取模型';$('#probeHelp').textContent='文本会发送极短消息；图片 API 会真实生成测试图，可能产生少量费用。'}syncModelFields(false)}
function syncModelFields(changed=false){const form=$('#form'),cap=form.elements.capability,protocol=form.elements.protocol,id=form.elements.model_identifier.value,official=isOfficialOpenAI(form);if(!changed&&looksLikeImageModel(id)&&cap.value!=='图片')cap.value='图片';const isImage=cap.value==='图片';if(official)protocol.value='codex';else{if(changed)protocol.value=isImage?'images':'openai';if(!isImage&&protocol.value==='images')protocol.value='openai';if(isImage){for(const key of ['image_generation_endpoint','image_edit_endpoint']){const input=form.elements[key];if(input.value&&!/^(https?:\/\/|\/)/i.test(input.value))input.value=''}}}['image_generation_endpoint','image_edit_endpoint'].forEach(k=>{const box=document.querySelector(`[data-field="${k}"]`);if(box)box.hidden=official||!isImage})}
async function removeItem(id){if(!await uiConfirm('确定删除这条配置吗？此操作不可撤销。',{title:'删除配置',confirmLabel:'永久删除',danger:true}))return;try{await api(`/api/${page}/${id}`,{method:'DELETE'});notice('配置已删除');await load()}catch(e){notice(e.message,true)}}
let openAIAccountPollTimer=null;
function accountPlanName(plan){return({pro:'Pro',prolite:'Pro',plus:'Plus',team:'Team',business:'Business',enterprise:'Enterprise',edu:'Edu',free:'Free',go:'Go'})[plan]||plan||'未知套餐'}
function renderOpenAIAccount(status){const box=$('#officialAccountStatus'),connect=$('#connectOpenAI'),disconnect=$('#disconnectOpenAI');if(!box)return;if(status.authorized){const plan=accountPlanName(status.plan_type);box.className='account-status connected';box.innerHTML=`<b>已连接 · ${esc(plan)}</b><span>${esc(status.email||'ChatGPT 账号')}</span>${status.is_pro?'':'<small>当前账号不是 Pro，仍可按该账号实际套餐使用。</small>'}`;connect.hidden=true;disconnect.hidden=false}else{box.className='account-status';box.innerHTML='<b>尚未连接 ChatGPT 账号</b><span>点击下方按钮，在 OpenAI 官方页面完成授权。</span>';connect.hidden=false;disconnect.hidden=true}}
async function loadOpenAIAccount(refresh=false){const box=$('#officialAccountStatus');if(!box)return;box.className='account-status loading';box.textContent='正在读取账号状态…';try{renderOpenAIAccount(await api('/api/openai-account/status'+(refresh?'?refresh=true':'')))}catch(e){box.className='account-status error';box.textContent=e.message}}
async function startOpenAIAccountLogin(){const popup=window.open('about:blank','openai-account-auth','width=720,height=780');try{const result=await api('/api/openai-account/login/start',{method:'POST',body:'{}'});if(!result.auth_url)throw new Error('授权服务没有返回登录地址');if(popup)popup.location.href=result.auth_url;else window.open(result.auth_url,'_blank');const box=$('#officialAccountStatus');box.className='account-status loading';box.textContent='等待你在 OpenAI 官方页面完成授权…';pollOpenAIAccountLogin(result.login_id,0)}catch(e){if(popup)popup.close();const box=$('#officialAccountStatus');box.className='account-status error';box.textContent=e.message}}
async function pollOpenAIAccountLogin(loginId,count){clearTimeout(openAIAccountPollTimer);try{const result=await api(`/api/openai-account/login/${encodeURIComponent(loginId)}`);if(result.completed){if(result.success){renderOpenAIAccount(result.account);probeMessage(`官方账号授权成功：${accountPlanName(result.account?.plan_type)}`)}else{const box=$('#officialAccountStatus');box.className='account-status error';box.textContent=result.error||'OpenAI 账号授权失败'}return}}catch(e){if(count>5){const box=$('#officialAccountStatus');if(box){box.className='account-status error';box.textContent=e.message}return}}if(count<200)openAIAccountPollTimer=setTimeout(()=>pollOpenAIAccountLogin(loginId,count+1),1500);else{const box=$('#officialAccountStatus');if(box){box.className='account-status error';box.textContent='授权等待超时，请重新连接。'}}}
async function disconnectOpenAIAccount(){if(!await uiConfirm('确定退出当前 OpenAI 官方账号吗？这会同时退出本机 Codex CLI 的该账号。',{title:'退出官方账号',confirmLabel:'确认退出',danger:true}))return;try{await api('/api/openai-account/logout',{method:'POST',body:'{}'});renderOpenAIAccount({authorized:false});probeMessage('OpenAI 官方账号已退出')}catch(e){probeMessage(e.message,true)}}
function probePayload(item=null){const form=$('#form');const value=name=>item?(item[name]||''):(form.elements[name]?.value||'');return{model_id:item?.id||editing||null,provider:value('provider')||'OpenAI Compatible',protocol:value('protocol')||'openai',capability:value('capability')||'文本',base_url:value('base_url'),api_key:value('api_key')||null,api_key_env:value('api_key_env'),model_identifier:value('model_identifier')||null,image_generation_endpoint:value('image_generation_endpoint')}}
function validateProbe(payload){if(payload.protocol==='codex'||payload.provider===OFFICIAL_OPENAI_PROVIDER)return true;if(!payload.base_url){probeMessage('请先填写接口地址。',true);return false}if(payload.capability==='图片'&&payload.image_generation_endpoint&&!/^(https?:\/\/|\/)/i.test(payload.image_generation_endpoint)){probeMessage('图片生成路径应填写 /images/generations 这类 API 路径，不能填写模型名或本地文件夹。',true);return false}return true}
function probeMessage(text,error=false){const box=$('#probeResult');if(box){box.hidden=false;box.textContent=text;box.className='probe-result '+(error?'error':'success')}else notice(text,error)}
async function discoverModels(){
  const session=editorSession;
  const payload=probePayload();if(!validateProbe(payload))return;
  probeMessage(payload.protocol==='codex'?'正在读取官方账号图片能力与模型目录…':'正在读取上游模型列表…');
  try{
    const r=await api('/api/models/discover',{method:'POST',body:JSON.stringify(payload)});
    if(editorSession!==session||!$('#editor').open)return;
    const select=$('#discovered'),available=r.models.filter(x=>x.selectable!==false);
    select.innerHTML=`<option value="">请选择模型（可选 ${available.length} 个）</option>`+r.models.map(x=>`<option value="${esc(x.id)}" ${x.selectable===false?'disabled':''}>${esc(x.display_name?x.display_name+' · '+x.id:x.id)}</option>`).join('');
    select.hidden=false;
    const current=$('#form').elements.model_identifier.value;
    if(available.some(x=>x.id===current))select.value=current;
    else if(!current&&payload.protocol==='codex'&&payload.capability==='图片'&&available.length){
      select.value=available[0].id;$('#form').elements.model_identifier.value=select.value;applyModelKind(select.value);
    }else select.value='';
    select.onchange=()=>{if(available.some(x=>x.id===select.value)){$('#form').elements.model_identifier.value=select.value;applyModelKind(select.value)}};
    const unavailable=current&&r.execution_mode==='codex_image_tool'&&!available.some(x=>x.id===current);
    probeMessage(`读取成功：共 ${r.count} 个目录项，可选 ${available.length} 个，耗时 ${r.elapsed_ms} ms。${r.message||''}${unavailable?' 当前填写的型号不能通过此通道指定，已保留原值，请调整配置。':''}`,!!unavailable);
  }catch(e){if(editorSession===session&&$('#editor').open)probeMessage(e.message,true)}
}
async function testConnection(){
  const payload=probePayload(),session=editorSession,modelId=editing;
  const current=()=>editorSession===session&&$('#editor').open;
  if(!validateProbe(payload))return;
  if(payload.capability==='图片'&&!await uiConfirm('图片检测会真实生成一张测试图片，可能产生少量费用。',{title:'确认图片模型检测',confirmLabel:'确认并检测'}))return;
  if(!current())return;
  probeMessage('正在执行一次真实的最小模型调用…');
  try{const r=await api('/api/models/test',{method:'POST',body:JSON.stringify(payload)});if(!current())return;probeMessage(r.message);if(modelId)await load();}
  catch(e){if(current()){probeMessage(e.message,true);if(modelId)await load();}}
}
async function quickTest(id){const item=(items.models||[]).find(x=>x.id===id);if(!item)return;if(item.capability==='图片'&&!await uiConfirm('图片检测会真实生成一张测试图片，可能产生少量费用。',{title:'确认图片模型检测',confirmLabel:'确认并检测'}))return;notice('正在进行实际连接检测…');try{const r=await api('/api/models/test',{method:'POST',body:JSON.stringify(probePayload(item))});notice(r.message);await load()}catch(e){notice(e.message,true);await load()}}
function notice(text,error=false){
  const n=$('#notice');clearTimeout(notice.timer);n.textContent=text;n.className='show'+(error?' error':'');
  n.setAttribute('role',error?'alert':'status');
  const dismiss=()=>{clearTimeout(notice.timer);n.className='';if(n.matches?.(':popover-open'))n.hidePopover();};
  if(error){const close=document.createElement('button');close.type='button';close.className='notice-dismiss';close.setAttribute('aria-label','关闭提示');close.textContent='×';close.onclick=dismiss;n.append(close);}
  // A modal dialog makes the rest of the document inert. Use the browser's
  // non-modal top layer so an operation error is still readable above it.
  if(typeof n.showPopover==='function'){n.setAttribute('popover','manual');if(!n.matches(':popover-open'))n.showPopover();}
  if(!error)notice.timer=setTimeout(dismiss,8000);
}
async function saveConfiguration(event) {
  event.preventDefault();
  if(configurationSavePending)return;
  configurationSavePending=true;
  const saveButton=$('#saveButton');saveButton.disabled=true;
  const session=editorSession,savePage=page,saveId=editing;
  try {
    const formData = new FormData(event.target);
    const payload = {};
    for (const field of pages[savePage].fields) {
      const [key, , type] = field;
      if (type === 'checkbox') payload[key] = formData.has(key);
      else if (type === 'multiselect' || type === 'planmulti') payload[key] = formData.getAll(key).map(Number);
      else if (type === 'platformmulti' || type === 'categorymulti') payload[key] = formData.getAll(key);
      else if (type === 'stringlist') payload[key] = (formData.get(key) || '').split(/[,，、\n]/).map(x => x.trim()).filter(Boolean);
      else if (type === 'hiddenlist' || type === 'specmanager') {
        try { payload[key] = JSON.parse(formData.get(key) || '[]'); } catch (_) { payload[key] = []; }
      }
      else if (type === 'number') payload[key] = formData.get(key) === '' ? null : Number(formData.get(key));
      else if (type === 'plan' || type === 'business') payload[key] = formData.get(key) ? Number(formData.get(key)) : null;
      else payload[key] = formData.get(key) || '';
    }
    await api('/api/' + savePage + (saveId ? '/' + saveId : ''), {
      method: saveId ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    });
    if(editorSession===session)$('#editor').close();
    notice('配置已保存');
    await load();
  } catch (error) {
    notice(error.message, true);
  } finally {
    configurationSavePending=false;
    saveButton.disabled=false;
  }
}
$('#nav').addEventListener('click',e=>{const b=e.target.closest('[data-page]');if(b){page=b.dataset.page;$('#search').value='';render()}});$('#add').onclick=()=>openEditor();$('#search').oninput=()=>render();$('#form').onsubmit=saveConfiguration;new MutationObserver(()=>{if(page==='businesses')prepareBusinessForm()}).observe($('#fields'),{childList:true});document.querySelectorAll('[data-close]').forEach(x=>x.onclick=()=>$('#editor').close());window.openEditor=openEditor;window.removeItem=removeItem;window.quickTest=quickTest;window.discoverModels=discoverModels;window.testConnection=testConnection;load();
