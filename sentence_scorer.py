# %%
import torch
import numpy
import torch.nn.functional as F
import ujson as json
import random
import time
import torch.optim as optim
import numpy as np
from joblib import Parallel, delayed
from torch.utils.data import DataLoader
from transformers import BertTokenizer, BertModel, BertForSequenceClassification, get_linear_schedule_with_warmup
from util import batch, flat_accuracy
import pickle


# %%
para_length_list = []
qapair_length_list = []
def process_article(qapair):
        single_data = {}                                     #single data의 attribute : question, answer
        single_data['question'] = qapair['question']
        single_data['answer'] = qapair['answer']
        paragraphs = qapair['context']                       #paragraphs는 context의 단락들로 구성
        spfacts = qapair['supporting_facts']                 #spfacts는 supporting_facts
        spfacts_titles = []
        for spfact in spfacts:
            if spfact[0] not in spfacts_titles:
                spfacts_titles.append(spfact[0])             #spfacts_titles는 supporting facts의 title들로 구성

        preprocessed_paragraphs=[]
        numlist = list(range(len(paragraphs)))

        # process 2 paragraphs with supporting sentences
        for para in paragraphs:           #단락들 중에 단락 각기 하나씩, 단락은 [제목, 내용] 으로 구성 para
            if para[0] in spfacts_titles:  # 단락 제목이 spfacts 제목 중에 포함되어있다면 같다면
                _para = {}                 #_para는 title, sentectes로 구성
                _para['title']=para[0]
                _para['sentences']=[]
                supporting_sentence_idx = []
                for spfact in spfacts:
                    if spfact[0] == para[0]: #spfact는 [제목, 몇 번째 문장에 중요한 정보 있는지 인덱스]로 구성
                        supporting_sentence_idx.append(spfact[1])#spfact의 문장들 중 몇 번째 문장인지
                for sentence_idx in range(len(para[1])):
                    single_sentence = {} # {'lable': 0} 이런 형태
                    if sentence_idx in supporting_sentence_idx: #supportingfacts에 있 중요한 정보 인덱스 
                        single_sentence['label'] = 1
                    else:
                        single_sentence['label'] = 0
                    single_sentence['sentence'] = para[1][sentence_idx] #single_sentence에는 모든 문장들을 리스트 형태로 저장(이럼 원본 context에서 title을 제거한 형태)
                    _para['sentences'].append(single_sentence) # 
                preprocessed_paragraphs.append(_para) #_para = {title: " " ,  sentences : [{'lable':0 , 'sentence' : '문장'} , ...] } 이런식으로
                numlist.remove(paragraphs.index(para))

                #최종 형태는 일단 supporting_facts에 해당하는 단락들만 _para에 저장됨, 단락을 구성하는 문장들은 전부 다 포함되는데 이 때 해당 문장이 핵심 문장인지 표시하는 lable이 있음
        """
        If possible, randomly sample 2 paragraphs without supporting sentences.
        Some article does not contain 10 paragraphs. Below is the number of examples with corresponding paragraph number.
        [0, 262, 156, 94, 88, 53, 77, 60, 48, 89609]
        """

        # supporting facts에 해당하지 않는 단락을 가능하다면 추가한다
        
        remaining_para_num = len(numlist)
        sampled_para_idx = random.sample(numlist,min(remaining_para_num, 2))

        for para_idx in sampled_para_idx:
            para = paragraphs[para_idx]
            _para = {}
            _para['title']=para[0]
            _para['sentences']=[]
            for sentence in para[1]:
                    single_sentence = {}
                    single_sentence['label'] = 0
                    single_sentence['sentence']=sentence
                    _para['sentences'].append(single_sentence)
            preprocessed_paragraphs.append(_para)

        single_data['paragraphs']=preprocessed_paragraphs
        return single_data


# %% 
def preprocess_file(filename):
    print('Preprocessing ', filename)
    data = json.load(open(filename, 'r'))

    preprocessed_datas = []

    outputs = Parallel(n_jobs=8, verbose=10)(delayed(process_article)(article) for article in data)
    preprocessed_datas = [e for e in outputs]
    print("Saving preprocessed_{}".format(filename))
    with open("preprocessed_"+filename, "w") as fh:
        json.dump(preprocessed_datas, fh)

def prepare_single_qapair(qapair, tokenizer):
    MAX_LEN = 512
    question = qapair['question']
    answer = qapair['answer']
    paragraphs = qapair['paragraphs']
    qapair_for_training = []

    line_before_para_tokens = tokenizer.convert_tokens_to_ids(tokenizer.tokenize("[CLS] " + question + " [SEP] "))
    line_after_para_tokens = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(" [SEP] " + answer + " [SEP]"))
    segment_before_para = [0 for _ in range(len(line_before_para_tokens))]
    segment_after_para = [0 for _ in range(len(line_after_para_tokens))]

    qapair_len = len(line_before_para_tokens) + len(line_after_para_tokens)
    for para in paragraphs:
        para_for_training= []
        indexed_tokens = []

        para_tokens = []

        for sentence in para['sentences']:
            sentence_token = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(sentence['sentence']))
            para_tokens += sentence_token

        qapair_len+=len(para_tokens)
        para_length_list.append(len(para_tokens))

        len_without_para_tokens = len(line_before_para_tokens) + len(line_after_para_tokens)
        # If a input has more than MAX_LEN tokens, we restrict the input to MAX_LEN.

        allowed_para_length = len(para_tokens)
        if len(para_tokens)+len_without_para_tokens> MAX_LEN:
            para_tokens = para_tokens[0:MAX_LEN-len_without_para_tokens]
            allowed_para_length = len(para_tokens)

        indexed_tokens = line_before_para_tokens + para_tokens + line_after_para_tokens
        attention_mask = [1 for _ in range(len(indexed_tokens))] + [0 for _ in range(MAX_LEN-len(indexed_tokens))]

        # Pad the tokens to MAX_LEN
        indexed_tokens += [0 for _ in range(MAX_LEN-len(indexed_tokens))]

        para_for_training.append(indexed_tokens)
        para_for_training.append(attention_mask)
        para_for_training_sentence_list=[]
        pos = 0

        for sentence in para['sentences']:
            sentence_token = tokenizer.convert_tokens_to_ids(tokenizer.tokenize(sentence['sentence']))
            segment_para = [0 for _ in range(allowed_para_length)]
            if pos+len(sentence_token) > allowed_para_length:
                if pos < allowed_para_length:
                    segment_para[pos:] = [1 for _ in range(allowed_para_length - pos)]
            else:
                segment_para[pos:pos+len(sentence_token)] = [1 for _ in range(len(sentence_token))]
                pos = pos + len(sentence_token)
            sentence_for_training = {}
            sentence_for_training['label']=sentence['label']
            sentence_segment_id = segment_before_para + segment_para + segment_after_para
            
            # Pad the tokens to MAX_LEN
            sentence_segment_id += [0 for _ in range(MAX_LEN-len(sentence_segment_id))]
            sentence_for_training['segment_id']= sentence_segment_id
            para_for_training_sentence_list.append(sentence_for_training)
            

        para_for_training.append(para_for_training_sentence_list)
        qapair_for_training.append(para_for_training)
    qapair_length_list.append(qapair_len)
    return qapair_for_training
            


def prepare_datas(preprocessed_file, data_category):
    # Perform tokenizing, Create attention mask & segment id for all datas.
    print('Loading preprocessed file...')
    data = json.load(open(preprocessed_file, 'r'))
    print('Loading BERT tokenizer...')
    tokenizer = BertTokenizer.from_pretrained('bert-base-cased')
    prepared_datas =[]
    outputs = Parallel(n_jobs=8, verbose=10)(delayed(prepare_single_qapair)(qapair, tokenizer) for qapair in data)      
    prepared_datas = [e for e in outputs]
    print("Saving {}_data".format(data_category))
    with open(data_category+"_data.json", "w") as fh:
        json.dump(prepared_datas, fh)

  # %%  
def train_and_evaluate_ras_model(train_dataset, dev_dataset):
    batch_size = 3
    num_epochs= 4
    MAX_batch_token_size = 5625
    accumulation_steps = 2

    print("sentence_scorer 실행")
    '''
    print("Preprocess training data")
    preprocess_file("hotpot_train_v1.1.json")
    

    print("Prepare training data")
    prepare_datas("preprocessed_hotpot_train_v1.1.json", "Training")


    para_length_list = []
    qapair_length_list = []

    print("Preprocess dev data")
    preprocess_file("hotpot_dev_distractor_v1.json")
    print("Prepare dev data")
    prepare_datas("preprocessed_hotpot_dev_distractor_v1.json", "Dev")

    return 0
    '''
    
    '''
    print("Loading training datasets..")
    train_dataset = json.load(open("Training_data.json", 'r'))

    print("Loading dev datasets..")
    dev_dataset = json.load(open("Dev_data.json"))
    '''

    sentence_scorer_model = BertForSequenceClassification.from_pretrained(
        "bert-base-cased",
        num_labels = 2, 
        output_attentions = False, 
        output_hidden_states = False, 
    )

    print("training start!")
    sentence_scorer_model.cuda()

    optimizer = optim.Adam(sentence_scorer_model.parameters(), lr=1e-5)
    total_training_steps = len(train_dataset) // batch_size if len(train_dataset) % batch_size ==0 else (len(train_dataset) // batch_size)+1
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=total_training_steps//10, num_training_steps= total_training_steps)

    # ============
    #   Training
    # ============

    print("training part!")
    training_stats = []

    for epoch in range(num_epochs):
        training_epoch_start_time = time.time()
        print("Shuffling dataset...")
        random.shuffle(train_dataset)
        print("")
        print('======== Epoch {:} / {:} ========'.format(epoch + 1, num_epochs))
        print('Training...')
        total_train_loss = 0
        sentence_scorer_model.train()
        step = 0
        for single_batch in batch(train_dataset, batch_size):

            # cap the batch size at 5625 tokens
            inputs_ids=[]
            attention_masks=[]
            segment_ids=[]
            labels=[]
            for question in single_batch:
                for para in question:
                    for sentence in para[2]:
                        inputs_ids.append(para[0])
                        attention_masks.append(para[1])
                        segment_ids.append(sentence['segment_id'])
                        labels.append(sentence['label'])
            current_batch_token_size = 0
            for sentence_token in inputs_ids:
                current_batch_token_size+=len(sentence_token)
            
            while current_batch_token_size > MAX_batch_token_size:
                drop_sentence_idx = random.randint(0, len(inputs_ids)-1)

                current_batch_token_size -=len(inputs_ids[drop_sentence_idx])
                del inputs_ids[drop_sentence_idx]
                del attention_masks[drop_sentence_idx]
                del segment_ids[drop_sentence_idx]
                del labels[drop_sentence_idx]
            
            sentence_scorer_model.zero_grad()
            loss = 0
            for i in range(accumulation_steps):
                num_elem = len(inputs_ids)
                train_size = 0
                if num_elem % accumulation_steps == 0:
                    train_size = num_elem // accumulation_steps
                else:
                    train_size = (num_elem // accumulation_steps) + 1
                b_inputs_ids = torch.Tensor(inputs_ids[i* train_size:min((i+1)*train_size, num_elem)]).cuda().long()
                b_segment_ids = torch.Tensor(segment_ids[i* train_size:min((i+1)*train_size, num_elem)]).cuda().long()
                b_attention_masks = torch.Tensor(attention_masks[i* train_size:min((i+1)*train_size, num_elem)]).cuda().long()
                b_labels = torch.Tensor(labels[i* train_size:min((i+1)*train_size, num_elem)]).cuda().long()
                outputs = sentence_scorer_model(input_ids = b_inputs_ids, token_type_ids=b_segment_ids, attention_mask=b_attention_masks, labels=b_labels)
                #print("error point!")
                #print(outputs.logits.detach().cpu().numpy())
                #print(outputs)
                loss = outputs.loss
                logits = outputs.logits

                loss = loss / accumulation_steps
                total_train_loss += loss.item()
                loss.backward()
                if (i+1) % accumulation_steps == 0:
                    optimizer.step()
                    scheduler.step()

            step+=1
            if step % 100 ==0 and step != 0:
                elapsed_epoch_time = time.time()-training_epoch_start_time
                print("Batch [ {} / {} ] , loss = {} , elapsed = {}".format(step, total_training_steps, loss.item(), elapsed_epoch_time))
        avg_train_loss = total_train_loss / step
        Training_time = time.time()-training_epoch_start_time
        print("Epoch {} average training loss : {}".format(epoch, avg_train_loss))
        print("Epoch {} took : ".format(Training_time))

        # ==============
        #   Validation
        # ==============

        print("Now validating...")
        sentence_scorer_model.eval()
        validation_epoch_start_time = time.time()

        total_eval_accuracy = 0
        total_eval_loss = 0
        step = 0
        for single_batch in batch(dev_dataset, batch_size):

            # cap the batch size at 5625 tokens
            inputs_ids=[]
            attention_masks=[]
            segment_ids=[]
            labels=[]
            for question in single_batch:
                for para in question:
                    for sentence in para[2]:
                        inputs_ids.append(para[0])
                        attention_masks.append(para[1])
                        segment_ids.append(sentence['segment_id'])
                        labels.append(sentence['label'])
            current_batch_token_size = 0
            for sentence_token in inputs_ids:
                current_batch_token_size+=len(sentence_token)
            
            while current_batch_token_size > MAX_batch_token_size:
                drop_sentence_idx = random.randint(0, len(inputs_ids)-1)

                current_batch_token_size -=len(inputs_ids[drop_sentence_idx])
                del inputs_ids[drop_sentence_idx]
                del attention_masks[drop_sentence_idx]
                del segment_ids[drop_sentence_idx]
                del labels[drop_sentence_idx]

            b_inputs_ids = torch.Tensor(inputs_ids).cuda().long()
            b_segment_ids = torch.Tensor(segment_ids).cuda().long()
            b_attention_masks = torch.Tensor(attention_masks).cuda().long()
            b_labels = torch.Tensor(labels).cuda().long()

            with torch.no_grad():
                outputs = sentence_scorer_model(input_ids = b_inputs_ids, token_type_ids=b_segment_ids, attention_mask=b_attention_masks, labels=b_labels)        
            
            loss = outputs.loss
            logits = outputs.logits
            total_eval_loss += loss.item()

            logits = logits.detach().cpu().numpy()
            label_ids = b_labels.to('cpu').numpy()

            total_eval_accuracy += flat_accuracy(logits, label_ids)

            step+=1

        avg_eval_loss = total_eval_loss / step
        avg_eval_accuracy = total_eval_accuracy / step
        Validation_time = time.time()-validation_epoch_start_time

        print("Epoch {} average validation loss : {}".format(epoch, avg_eval_loss))
        print("Epoch {} average validation accuracy : {}".format(epoch, avg_eval_accuracy))
        
        training_stats.append(
            {
                'epoch': epoch+1,
                'Training_Loss': avg_train_loss,
                'Valid_Loss': avg_eval_loss,
                'Valid_Accuracy': avg_eval_accuracy,
                'Training_Time': Training_time,
                'Validation_Time': Validation_time
            }
        )

      #Save the training stats
        print("Saving training stats...")
        with open("Training_stats_ras.json", "w") as fh:
          json.dump(training_stats, fh)

        # Save the fine-tuned model
        print("Saving the fine-tuned model..")
        sentence_scorer_model.save_pretrained('./model/ras/')
        print("Training complete!")



# %% 
dev_data = json.load(open("Dev_data.json", 'r'))
#dev_data_wa = json.load(open("Dev_data_wa.json", 'r'))

training_data = json.load(open("short_training_data.json", 'r'))
#training_data_wa = json.load(open("short_training_data_wa.json", 'r'))
# %%
train_and_evaluate_ras_model(training_data, dev_data)
# %%
